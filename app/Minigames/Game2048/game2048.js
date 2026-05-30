(() => {
  const root = document.querySelector("[data-2048-app]");
  if (!root) {
    return;
  }

  const boardEl = root.querySelector("[data-2048-board]");
  const scoreEl = root.querySelector("[data-2048-score]");
  const movesEl = root.querySelector("[data-2048-moves]");
  const maxTileEl = root.querySelector("[data-2048-max-tile]");
  const bestScoreEl = root.querySelector("[data-2048-best-score]");
  const statusEl = root.querySelector("[data-2048-status]");
  const newBtn = root.querySelector("[data-2048-new]");
  const historyBodyEl = root.querySelector("[data-2048-history-body]");
  const directionButtons = Array.from(root.querySelectorAll("[data-2048-dir]"));

  if (!boardEl || !scoreEl || !movesEl || !maxTileEl || !bestScoreEl || !statusEl || !newBtn || !historyBodyEl || directionButtons.length !== 4) {
    return;
  }

  const historyUrl = String(root.dataset.historyUrl || "").trim();
  const recordUrl = String(root.dataset.recordUrl || historyUrl).trim();
  const deleteUrl = String(root.dataset.deleteUrl || historyUrl).trim();
  const boardSize = 4;
  const boardCells = boardSize * boardSize;
  const targetTile = 2048;

  const keyToDirection = {
    ArrowUp: "up",
    ArrowDown: "down",
    ArrowLeft: "left",
    ArrowRight: "right",
    w: "up",
    W: "up",
    s: "down",
    S: "down",
    a: "left",
    A: "left",
    d: "right",
    D: "right",
  };

  const lineSpecs = {
    left: [
      [0, 1, 2, 3],
      [4, 5, 6, 7],
      [8, 9, 10, 11],
      [12, 13, 14, 15],
    ],
    right: [
      [3, 2, 1, 0],
      [7, 6, 5, 4],
      [11, 10, 9, 8],
      [15, 14, 13, 12],
    ],
    up: [
      [0, 4, 8, 12],
      [1, 5, 9, 13],
      [2, 6, 10, 14],
      [3, 7, 11, 15],
    ],
    down: [
      [12, 8, 4, 0],
      [13, 9, 5, 1],
      [14, 10, 6, 2],
      [15, 11, 7, 3],
    ],
  };

  let board = Array.from({ length: boardCells }, () => 0);
  let score = 0;
  let moves = 0;
  let maxTile = 0;
  let bestScore = 0;
  let bestTile = 0;
  let historyRows = [];
  let startedAtIso = null;
  let seededAtIso = new Date().toISOString();
  let keyboardMoves = 0;
  let clickMoves = 0;
  let ended = false;
  let runSaved = false;

  const nowIso = () => new Date().toISOString();

  const updateStats = () => {
    scoreEl.textContent = String(score);
    movesEl.textContent = String(moves);
    maxTileEl.textContent = String(maxTile);
    bestScoreEl.textContent = String(bestScore);
  };

  const tileClass = (value) => {
    if (value <= 0) {
      return "g2048-tile is-empty";
    }
    return `g2048-tile value-${value}`;
  };

  const renderBoard = () => {
    const frag = document.createDocumentFragment();
    for (const value of board) {
      const cell = document.createElement("div");
      cell.className = tileClass(Number(value) || 0);
      cell.setAttribute("role", "gridcell");
      if (value > 0) {
        cell.textContent = String(value);
      } else {
        cell.textContent = "";
        cell.setAttribute("aria-label", "Empty tile");
      }
      frag.appendChild(cell);
    }
    boardEl.replaceChildren(frag);
  };

  const renderHistory = () => {
    if (!Array.isArray(historyRows) || historyRows.length === 0) {
      historyBodyEl.innerHTML = "<tr><td colspan=\"6\" class=\"cell-data\">No recorded runs yet.</td></tr>";
      return;
    }

    const rows = historyRows.map((row) => {
      const state = String(row.state || "unknown").toLowerCase();
      const runId = String(row.id || "").trim();
      const stateClass = state === "won" ? "badge-done" : "badge-excluded";
      const finishedAt = new Date(String(row.finished_at || row.saved_at || ""));
      const when = Number.isNaN(finishedAt.getTime()) ? "-" : finishedAt.toLocaleString();
      const manageCell = runId
        ? `<td class="cell-data g2048-col-manage"><button type="button" class="btn-mini btn-danger g2048-history-delete" data-2048-delete="${runId}">Delete</button></td>`
        : "<td class=\"cell-data g2048-col-manage\">-</td>";
      return [
        "<tr class=\"data-row\">",
        `<td class="cell-data">${when}</td>`,
        `<td class="cell-data"><span class="badge ${stateClass}">${state}</span></td>`,
        `<td class="cell-data">${Number(row.moves || 0)}</td>`,
        `<td class="cell-data">${Number(row.score || 0)}</td>`,
        `<td class="cell-data">${Number(row.max_tile || 0)}</td>`,
        manageCell,
        "</tr>",
      ].join("");
    }).join("");

    historyBodyEl.innerHTML = rows;
  };

  const applyHistoryPayload = (payload) => {
    if (!payload || typeof payload !== "object") {
      return;
    }
    historyRows = Array.isArray(payload.history) ? payload.history : [];
    bestScore = Number(payload.best_score || 0);
    bestTile = Number(payload.best_tile || 0);
    renderHistory();
    updateStats();
  };

  const loadHistory = async () => {
    if (!historyUrl) {
      historyRows = [];
      bestScore = 0;
      bestTile = 0;
      renderHistory();
      updateStats();
      return;
    }

    try {
      const response = await fetch(historyUrl, {
        method: "GET",
        headers: { Accept: "application/json" },
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) {
        throw new Error(`History fetch failed (${response.status})`);
      }
      const payload = await response.json();
      applyHistoryPayload(payload);
    } catch (_err) {
      historyRows = [];
      bestScore = 0;
      bestTile = 0;
      renderHistory();
      updateStats();
    }
  };

  const deleteRun = async (runId) => {
    if (!deleteUrl) {
      return;
    }

    try {
      const response = await fetch(`${deleteUrl}/${encodeURIComponent(runId)}`, {
        method: "DELETE",
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (!response.ok) {
        throw new Error(`History delete failed (${response.status})`);
      }
      const payload = await response.json();
      applyHistoryPayload(payload);
    } catch (_err) {
      // History delete failures should not break gameplay.
    }
  };

  const postRun = async (state) => {
    if (!recordUrl || runSaved || moves <= 0) {
      return;
    }
    runSaved = true;

    const payload = {
      state,
      started_at: String(startedAtIso || seededAtIso),
      finished_at: nowIso(),
      moves,
      score,
      max_tile: maxTile,
      board_size: boardSize,
      input_counts: {
        keyboard: keyboardMoves,
        click: clickMoves,
      },
      board_end: board.slice(),
    };

    try {
      const response = await fetch(recordUrl, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        credentials: "same-origin",
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        throw new Error(`History save failed (${response.status})`);
      }
      const body = await response.json();
      applyHistoryPayload(body);
    } catch (_err) {
      // History save failures should not break gameplay.
    }
  };

  const spawnRandomTile = () => {
    const empties = [];
    for (let i = 0; i < board.length; i += 1) {
      if (board[i] === 0) {
        empties.push(i);
      }
    }
    if (empties.length === 0) {
      return false;
    }
    const index = empties[Math.floor(Math.random() * empties.length)];
    board[index] = Math.random() < 0.9 ? 2 : 4;
    return true;
  };

  const reduceLine = (values) => {
    const compact = values.filter((value) => value > 0);
    const reduced = [];
    let gained = 0;

    for (let i = 0; i < compact.length; i += 1) {
      const value = compact[i];
      const next = compact[i + 1];
      if (value > 0 && value === next) {
        const merged = value * 2;
        reduced.push(merged);
        gained += merged;
        i += 1;
      } else {
        reduced.push(value);
      }
    }

    while (reduced.length < boardSize) {
      reduced.push(0);
    }

    const changed = reduced.some((value, index) => value !== values[index]);
    return {
      values: reduced,
      gained,
      changed,
    };
  };

  const hasMovesAvailable = () => {
    for (let i = 0; i < board.length; i += 1) {
      const value = board[i];
      if (value === 0) {
        return true;
      }
      const row = Math.floor(i / boardSize);
      const col = i % boardSize;
      if (col < boardSize - 1 && board[i + 1] === value) {
        return true;
      }
      if (row < boardSize - 1 && board[i + boardSize] === value) {
        return true;
      }
    }
    return false;
  };

  const attemptMove = async (direction, inputKind) => {
    if (!lineSpecs[direction] || ended) {
      return;
    }

    let anyChanged = false;
    let scoreGain = 0;

    for (const line of lineSpecs[direction]) {
      const current = line.map((index) => board[index]);
      const reduced = reduceLine(current);
      if (reduced.changed) {
        anyChanged = true;
      }
      scoreGain += reduced.gained;
      for (let i = 0; i < line.length; i += 1) {
        board[line[i]] = reduced.values[i];
      }
    }

    if (!anyChanged) {
      return;
    }

    if (!startedAtIso) {
      startedAtIso = nowIso();
    }

    moves += 1;
    if (inputKind === "keyboard") {
      keyboardMoves += 1;
    } else {
      clickMoves += 1;
    }
    score += scoreGain;
    spawnRandomTile();
    maxTile = Math.max(...board, 0);

    renderBoard();
    updateStats();

    if (maxTile >= targetTile) {
      ended = true;
      statusEl.textContent = "You reached 2048!";
      await postRun("won");
      return;
    }

    if (!hasMovesAvailable()) {
      ended = true;
      statusEl.textContent = "No moves left";
      await postRun("lost");
      return;
    }

    statusEl.textContent = "In progress";
  };

  const newGame = () => {
    board = Array.from({ length: boardCells }, () => 0);
    score = 0;
    moves = 0;
    maxTile = 0;
    startedAtIso = null;
    seededAtIso = nowIso();
    keyboardMoves = 0;
    clickMoves = 0;
    ended = false;
    runSaved = false;

    spawnRandomTile();
    spawnRandomTile();
    maxTile = Math.max(...board, 0);
    statusEl.textContent = "Ready (no timer)";

    renderBoard();
    updateStats();
  };

  newBtn.addEventListener("click", () => {
    newGame();
  });

  for (const button of directionButtons) {
    button.addEventListener("click", () => {
      const direction = String(button.getAttribute("data-2048-dir") || "").toLowerCase();
      void attemptMove(direction, "click");
    });
  }

  historyBodyEl.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const button = target.closest("[data-2048-delete]");
    if (!button || !(button instanceof HTMLElement)) {
      return;
    }
    const runId = String(button.getAttribute("data-2048-delete") || "").trim();
    if (!runId) {
      return;
    }
    if (!window.confirm("Delete this run from history?")) {
      return;
    }
    void deleteRun(runId);
  });

  window.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) {
      return;
    }

    const target = event.target;
    if (target instanceof HTMLElement) {
      const tag = target.tagName.toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") {
        return;
      }
    }

    const direction = keyToDirection[event.key];
    if (!direction) {
      return;
    }

    event.preventDefault();
    void attemptMove(direction, "keyboard");
  });

  const init = async () => {
    await loadHistory();
    newGame();
  };

  void init();
})();
