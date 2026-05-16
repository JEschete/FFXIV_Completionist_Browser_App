"""FFXIV Completion Tracker — FastAPI + HTMX.

Navigation mirrors the workbook: a menu tree in the sidebar, breadcrumb trail,
category-grid views for menu nodes and data-table views for content sheets.
Row state is toggled in place via HTMX; prerequisite chains are first-class.
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parent.parent))

from contextlib import asynccontextmanager

from app import db, progress_io


def reconcile_progress_sidecars() -> None:
    """Make the DB match the per-character JSON sidecars (the source of truth
    for progress). One-time bootstraps any character that has DB progress but
    no sidecar yet; otherwise replays each sidecar's entries onto the current
    run's character_progress table. Detail logic lives in progress_io."""
    conn = db.get_connection()
    try:
        run_id = db.latest_run_id(conn)
        if run_id is None:
            print("Progress reconcile: skipped — no ingest run found "
                  "(run scripts/prep_xlsx_to_sqlite.py to populate the DB)")
            return
        report = progress_io.reconcile_all(conn, run_id)
        if report.characters:
            print("Progress reconcile:")
            print(report.summary())
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    reconcile_progress_sidecars()
    yield


app = FastAPI(title="FFXIV Completion Tracker", lifespan=lifespan)

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

CHAR_COOKIE = "ffxiv_character"


# --- request context --------------------------------------------------------

def cookie_character_id(request: Request) -> int | None:
    raw = request.cookies.get(CHAR_COOKIE)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def set_char_cookie(response, character_id: int) -> None:
    response.set_cookie(
        CHAR_COOKIE, str(character_id), max_age=60 * 60 * 24 * 365, samesite="lax"
    )


class Ctx:
    """Per-request context.

    `full=True` (default) loads the nav tree, rollups, and character list — what
    a page render needs. `full=False` is the lightweight path for HTMX partial
    endpoints; it skips the expensive sheet/rollup/tree work that a single-row
    write or fragment doesn't need.
    """

    def __init__(self, request: Request, *, full: bool = True):
        self.request = request
        self.conn = db.get_connection()
        run_id = db.latest_run_id(self.conn)
        if run_id is None:
            raise HTTPException(503, "No ingest run found. Run the prep script first.")
        self.run_id: int = run_id
        self.character = db.resolve_active_character(
            self.conn, cookie_character_id(request)
        )
        self.character_id = int(self.character["id"])
        self.starting_class: str | None = (
            self.character["starting_class"]
            if "starting_class" in self.character.keys() else None
        )
        self.full = full
        if full:
            self.characters = db.fetch_characters(self.conn)
            self.sheets = db.fetch_all_sheets(self.conn, self.run_id)
            self.sheets_by_name = {s["sheet_name"]: dict(s) for s in self.sheets}
            self.rollups = db.sheet_rollups(
                self.conn, self.run_id, self.character_id, self.starting_class
            )
            self.tree, self.overall = db.build_nav_tree(self.sheets, self.rollups)

    def close(self) -> None:
        self.conn.close()

    def require_content_sheet(self, sheet_name: str) -> dict:
        """Lightweight single-sheet lookup for partial endpoints."""
        if self.full:
            sheet = self.sheets_by_name.get(sheet_name)
        else:
            row = db.fetch_sheet(self.conn, self.run_id, sheet_name)
            sheet = dict(row) if row is not None else None
        if sheet is None:
            raise HTTPException(404, f"Unknown sheet: {sheet_name}")
        if sheet["is_menu"]:
            raise HTTPException(400, "Not a content sheet")
        return sheet

    def base_context(self) -> dict:
        return {
            "request": self.request,
            "characters": self.characters,
            "character": self.character,
            "tree": self.tree,
            "overall": self.overall,
            "overall_pct": db.pct(self.overall),
        }

    def header_context(self, sheet_name: str | None) -> dict:
        node = db.find_node(self.tree, sheet_name) if sheet_name else None
        roll = self.rollups.get(sheet_name) if sheet_name else None
        return {
            "overall": self.overall,
            "overall_pct": db.pct(self.overall),
            "cur_node": node,
            "cur_roll": roll,
            "cur_pct": db.pct(roll) if roll else 0.0,
            "cur_sheet": sheet_name,
        }

    def render(self, template: str, extra: dict) -> HTMLResponse:
        active = extra.get("active_sheet")
        db.mark_active_path(self.tree, active)
        ctx = {**self.base_context(), **self.header_context(active), **extra}
        resp = templates.TemplateResponse(self.request, template, ctx)
        set_char_cookie(resp, self.character_id)
        return resp


# --- pages ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    ctx = Ctx(request)
    try:
        # top-level menu cards + a few "needs attention" chains
        cards = []
        for node in ctx.tree:
            cards.append({
                "sheet_name": node["sheet_name"],
                "title": node["title"],
                "roll": node["roll"],
                "pct": node["pct"],
                "children": len(node["children"]),
            })
        chains = db.chain_sheets_overview(
            ctx.conn, ctx.run_id, ctx.character_id, ctx.starting_class
        )[:6]
        return ctx.render("dashboard.html", {
            "cards": cards,
            "chains": chains,
            "active_sheet": None,
        })
    finally:
        ctx.close()


@app.get("/browse/{sheet_name}", response_class=HTMLResponse)
def browse(request: Request, sheet_name: str, q: str = "", state: str = "all"):
    ctx = Ctx(request)
    try:
        sheet = ctx.sheets_by_name.get(sheet_name)
        if sheet is None:
            raise HTTPException(404, f"Unknown sheet: {sheet_name}")

        crumbs = db.breadcrumb_path(ctx.sheets_by_name, sheet_name)
        node = db.find_node(ctx.tree, sheet_name)

        if sheet["is_menu"]:
            # category-grid view: child cards with aggregated progress,
            # grouped by parent_menu_section (the workbook column header
            # the child came from). Sections appear in workbook order;
            # any child without a section goes to a final "Other" bucket.
            # Group child cards by parent_menu_section (the workbook column
            # they came from). Renamed from "items" to "cards" so Jinja's
            # attribute lookup doesn't collide with the dict ``.items()``
            # method when iterating ``group.cards`` in the template.
            section_order: list[str | None] = []
            section_cards: dict[str | None, list[dict]] = {}
            for child in (node["children"] if node else []):
                card = {
                    "sheet_name": child["sheet_name"],
                    "title": child["title"],
                    "is_menu": child["is_menu"],
                    "roll": child["roll"],
                    "pct": child["pct"],
                    "children": len(child["children"]),
                }
                section = child.get("parent_menu_section")
                if section not in section_cards:
                    section_cards[section] = []
                    section_order.append(section)
                section_cards[section].append(card)

            sections = [
                {"label": s, "cards": section_cards[s]}
                for s in section_order if s is not None
            ]
            # children with no detected section render last under a soft heading
            if None in section_cards:
                sections.append({"label": None, "cards": section_cards[None]})
            children_flat = [c for s in sections for c in s["cards"]]
            return ctx.render("menu.html", {
                "sheet": sheet,
                "crumbs": crumbs,
                "sections": sections,
                "children": children_flat,  # kept for the page-sub count
                "node": node,
                "active_sheet": sheet_name,
            })

        # content sheet: data-table view grouped by section
        rows = db.fetch_rows(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name,
            q=q, state=state, starting_class=ctx.starting_class,
        )
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name, ctx.starting_class
        )
        for row in rows:
            row["chain_info"] = flags.get(row["row_index"])
        groups = db.group_rows_by_section(rows)
        roll = ctx.rollups.get(sheet_name, db._empty_roll())
        import json as _json
        return ctx.render("sheet.html", {
            "sheet": sheet,
            "columns": _json.loads(sheet["data_columns_json"]),
            "crumbs": crumbs,
            "groups": groups,
            "roll": roll,
            "pct": db.pct(roll),
            "q": q,
            "state": state,
            "shown": sum(len(g["rows"]) for g in groups),
            "active_sheet": sheet_name,
        })
    finally:
        ctx.close()


@app.get("/chains", response_class=HTMLResponse)
def chains_overview(request: Request):
    ctx = Ctx(request)
    try:
        chains = db.chain_sheets_overview(
            ctx.conn, ctx.run_id, ctx.character_id, ctx.starting_class
        )
        return ctx.render("chains.html", {"chains": chains, "active_sheet": None})
    finally:
        ctx.close()


@app.get("/characters", response_class=HTMLResponse)
def characters_page(request: Request, error: str = ""):
    ctx = Ctx(request)
    try:
        return ctx.render("characters.html", {
            "error": error,
            "starting_classes": db.STARTING_CLASSES,
            "active_sheet": None,
        })
    finally:
        ctx.close()


# --- HTMX partials ----------------------------------------------------------

def _render_row(
    ctx: Ctx,
    sheet: dict,
    row_index: int,
    columns: list,
    chain_info: dict | None = None,
    *,
    oob: bool = False,
) -> str:
    row = db.fetch_row(
        ctx.conn, ctx.run_id, ctx.character_id, sheet["sheet_name"], row_index,
        ctx.starting_class,
    )
    if row is None:
        raise HTTPException(404, "Row not found")
    return templates.get_template("partials/row.html").render(
        request=ctx.request,
        sheet=sheet,
        columns=columns,
        row=row,
        chain=chain_info,
        oob=oob,
    )


@app.post("/api/toggle", response_class=HTMLResponse)
def api_toggle(
    request: Request,
    sheet_name: str = Form(...),
    row_index: int = Form(...),
):
    """Toggle a row's state. For chain rows transitioning todo→done, also
    auto-cascade the prerequisites; cascaded rows ride along as out-of-band
    `<tr>` fragments so HTMX patches them in place."""
    ctx = Ctx(request, full=False)
    try:
        sheet = ctx.require_content_sheet(sheet_name)
        _, changed = db.toggle_row(
            ctx.conn, ctx.character_id, ctx.run_id, sheet_name, row_index,
            ctx.starting_class,
        )
        import json as _json
        columns = _json.loads(sheet["data_columns_json"])
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name,
            ctx.starting_class,
        )
        # the clicked row replaces #row-{row_index} via the main swap; the
        # rest piggyback as OOB swaps keyed by their own ids
        parts: list[str] = []
        for idx in sorted(set(changed) - {row_index}):
            parts.append(_render_row(ctx, sheet, idx, columns, flags.get(idx), oob=True))
        parts.append(_render_row(ctx, sheet, row_index, columns, flags.get(row_index)))
        resp = HTMLResponse("\n".join(parts))
        resp.headers["HX-Trigger"] = "progress-changed"
        return resp
    finally:
        ctx.close()


@app.post("/api/set-value", response_class=HTMLResponse)
def api_set_value(
    request: Request,
    sheet_name: str = Form(...),
    row_index: int = Form(...),
    percent: float = Form(...),
):
    """Set a value-row's numeric level (0-100); state is derived."""
    ctx = Ctx(request, full=False)
    try:
        sheet = ctx.require_content_sheet(sheet_name)
        db.set_row_value(
            ctx.conn, ctx.character_id, ctx.run_id, sheet_name, row_index,
            percent, starting_class=ctx.starting_class,
        )
        import json as _json
        columns = _json.loads(sheet["data_columns_json"])
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name,
            ctx.starting_class,
        )
        body = _render_row(ctx, sheet, row_index, columns, flags.get(row_index))
        resp = HTMLResponse(body)
        resp.headers["HX-Trigger"] = "progress-changed"
        return resp
    finally:
        ctx.close()


@app.post("/api/toggle-excluded", response_class=HTMLResponse)
def api_toggle_excluded(
    request: Request,
    sheet_name: str = Form(...),
    row_index: int = Form(...),
):
    """Two-state exclude toggle for value rows."""
    ctx = Ctx(request, full=False)
    try:
        sheet = ctx.require_content_sheet(sheet_name)
        db.toggle_excluded(
            ctx.conn, ctx.character_id, ctx.run_id, sheet_name, row_index,
            ctx.starting_class,
        )
        import json as _json
        columns = _json.loads(sheet["data_columns_json"])
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name,
            ctx.starting_class,
        )
        body = _render_row(ctx, sheet, row_index, columns, flags.get(row_index))
        resp = HTMLResponse(body)
        resp.headers["HX-Trigger"] = "progress-changed"
        return resp
    finally:
        ctx.close()


@app.post("/api/set-state", response_class=HTMLResponse)
def api_set_state(
    request: Request,
    sheet_name: str = Form(...),
    row_index: int = Form(...),
    state: str = Form(...),
):
    ctx = Ctx(request, full=False)
    try:
        sheet = ctx.require_content_sheet(sheet_name)
        db.set_row_state(
            ctx.conn, ctx.character_id, ctx.run_id, sheet_name, row_index, state,
            starting_class=ctx.starting_class,
        )
        import json as _json
        columns = _json.loads(sheet["data_columns_json"])
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name,
            ctx.starting_class,
        )
        body = _render_row(ctx, sheet, row_index, columns, flags.get(row_index))
        resp = HTMLResponse(body)
        resp.headers["HX-Trigger"] = "progress-changed"
        return resp
    finally:
        ctx.close()


@app.post("/api/complete-chain", response_class=HTMLResponse)
def api_complete_chain(
    request: Request,
    sheet_name: str = Form(...),
    row_index: int = Form(...),
):
    """Mark a row and all its prerequisites done. Returns out-of-band row
    fragments so HTMX patches every changed `<tr>` in place — no page reload."""
    ctx = Ctx(request, full=False)
    try:
        sheet = ctx.require_content_sheet(sheet_name)
        changed = db.complete_with_prerequisites(
            ctx.conn, ctx.character_id, ctx.run_id, sheet_name, row_index,
            ctx.starting_class,
        )
        import json as _json
        columns = _json.loads(sheet["data_columns_json"])
        # one flags fetch covers every row we need to re-render
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name,
            ctx.starting_class,
        )
        # also re-render visible neighbors that may have unblocked. Cheap: just
        # the row right after each changed one (same section).
        to_render = sorted(set(changed))
        parts = [
            _render_row(ctx, sheet, idx, columns, flags.get(idx), oob=True)
            for idx in to_render
        ]
        resp = HTMLResponse("\n".join(parts))
        resp.headers["HX-Trigger"] = "progress-changed"
        return resp
    finally:
        ctx.close()


@app.get("/api/chain/{sheet_name}/{row_index}", response_class=HTMLResponse)
def api_chain(request: Request, sheet_name: str, row_index: int):
    ctx = Ctx(request, full=False)
    try:
        sheet = ctx.require_content_sheet(sheet_name)
        row = db.fetch_row(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name, row_index,
            ctx.starting_class,
        )
        chain = db.fetch_chain(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name, row_index,
            ctx.starting_class,
        )
        return templates.TemplateResponse(
            ctx.request,
            "partials/chain_panel.html",
            {"request": request, "sheet": sheet, "row": row, "chain": chain},
        )
    finally:
        ctx.close()


@app.get("/api/progress-header", response_class=HTMLResponse)
def api_progress_header(request: Request, sheet_name: str = ""):
    """Re-rendered after a toggle so the overall + sheet bars stay live."""
    ctx = Ctx(request)
    try:
        header = ctx.header_context(sheet_name or None)
        return templates.TemplateResponse(
            ctx.request, "partials/progress_header.html",
            {"request": request, **header},
        )
    finally:
        ctx.close()


@app.get("/api/search", response_class=HTMLResponse)
def api_search(request: Request, q: str = ""):
    ctx = Ctx(request)
    try:
        results = db.search_nodes(
            ctx.conn, ctx.run_id, ctx.character_id, q,
            starting_class=ctx.starting_class,
        )
        return templates.TemplateResponse(
            ctx.request,
            "partials/search_results.html",
            {"request": request, "results": results, "q": q},
        )
    finally:
        ctx.close()


# --- character actions ------------------------------------------------------

@app.post("/characters/create")
def character_create(request: Request, name: str = Form(...)):
    conn = db.get_connection()
    try:
        try:
            cid = db.create_character(conn, name)
        except ValueError as exc:
            return RedirectResponse(f"/characters?error={quote(str(exc))}", status_code=303)
        except Exception:
            return RedirectResponse(
                f"/characters?error={quote('Name already exists')}", status_code=303
            )
    finally:
        conn.close()
    resp = RedirectResponse("/", status_code=303)
    set_char_cookie(resp, cid)
    return resp


@app.post("/characters/select")
def character_select(character_id: int = Form(...), next_url: str = Form("/")):
    target = next_url if next_url.startswith("/") else "/"
    resp = RedirectResponse(target, status_code=303)
    set_char_cookie(resp, character_id)
    return resp


@app.post("/characters/set-class")
def character_set_class(
    character_id: int = Form(...),
    starting_class: str = Form(""),
):
    cls = starting_class.strip().upper() or None
    conn = db.get_connection()
    try:
        try:
            db.set_character_class(conn, character_id, cls)
        except ValueError as exc:
            return RedirectResponse(
                f"/characters?error={quote(str(exc))}", status_code=303
            )
    finally:
        conn.close()
    return RedirectResponse("/characters", status_code=303)


@app.post("/characters/delete")
def character_delete(request: Request, character_id: int = Form(...)):
    conn = db.get_connection()
    try:
        try:
            fallback = db.delete_character(conn, character_id)
        except ValueError as exc:
            return RedirectResponse(
                f"/characters?error={quote(str(exc))}", status_code=303
            )
    finally:
        conn.close()
    active = cookie_character_id(request)
    resp = RedirectResponse("/characters", status_code=303)
    set_char_cookie(resp, fallback if active == character_id else (active or fallback))
    return resp


# --- export -----------------------------------------------------------------

@app.get("/export/current.csv")
def export_csv(request: Request):
    ctx = Ctx(request)
    try:
        rows = db.fetch_export_rows(
            ctx.conn, ctx.run_id, ctx.character_id, ctx.starting_class
        )
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["character", "sheet", "section", "row_index", "label", "state",
                    "progress_percent"])
        for r in rows:
            w.writerow([
                ctx.character["name"], r["sheet_name"], r["section_label"],
                r["row_index"], r["label"], r["state"], r["progress_percent"],
            ])
        data = io.BytesIO(buf.getvalue().encode("utf-8"))
        fname = f"ffxiv_{ctx.character['name'].replace(' ', '_')}.csv"
        return StreamingResponse(
            data, media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    finally:
        ctx.close()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
