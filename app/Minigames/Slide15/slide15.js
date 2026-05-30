(() => {
  const root = document.querySelector("[data-slide15-app]");
  if (!root) {
    return;
  }

  const boardEl = root.querySelector("[data-slide15-board]");
  const clockEl = root.querySelector("[data-slide15-clock]");
  const actionsEl = root.querySelector("[data-slide15-actions]");
  const apsEl = root.querySelector("[data-slide15-aps]");
  const scoreEl = root.querySelector("[data-slide15-score]");
  const bestEl = root.querySelector("[data-slide15-best]");
  const statusEl = root.querySelector("[data-slide15-status]");
  const restartBtn = root.querySelector("[data-slide15-restart]");
  const newBtn = root.querySelector("[data-slide15-new]");
  const historyBodyEl = root.querySelector("[data-slide15-history-body]");

  if (!boardEl || !clockEl || !actionsEl || !apsEl || !scoreEl || !bestEl || !statusEl || !restartBtn || !newBtn || !historyBodyEl) {
    return;
  }

  const historyUrl = String(root.dataset.historyUrl || "").trim();
  const recordUrl = String(root.dataset.recordUrl || historyUrl).trim();
  const deleteUrl = String(root.dataset.deleteUrl || historyUrl).trim();
  const boardSize = 4;
  const scrambleDepth = 180;
  const solvedBoard = Array.from({ length: (boardSize * boardSize) - 1 }, (_v, idx) => idx + 1).concat(0);

  const keyDelta = {
    ArrowUp: [1, 0],
    ArrowDown: [-1, 0],
    ArrowLeft: [0, 1],
    ArrowRight: [0, -1],
    w: [1, 0],
    W: [1, 0],
    s: [-1, 0],
    S: [-1, 0],
    a: [0, 1],
    A: [0, 1],
    d: [0, -1],
    D: [0, -1],
  };

  let board = solvedBoard.slice();
  let boardStart = solvedBoard.slice();
  let startedAtMs = null;
  let actions = 0;
  let keyboardActions = 0;
  let clickActions = 0;
  let solved = false;
  let ticker = null;
  let bestScore = 0;
  let historyRows = [];

  const nowIso = () => new Date().toISOString();

  const isSolvedBoard = (cells) => {
    if (!Array.isArray(cells) || cells.length !== solvedBoard.length) {
      return false;
    }
    for (let i = 0; i < solvedBoard.length; i += 1) {
      if (Number(cells[i]) !== solvedBoard[i]) {
        return false;
      }
    }
    return true;
  };

  const formatClock = (elapsedMs) => {
    const totalMs = Math.max(0, Math.floor(elapsedMs));
    const minutes = Math.floor(totalMs / 60000);
    const seconds = Math.floor((totalMs % 60000) / 1000);
    const hundredths = Math.floor((totalMs % 1000) / 10);
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(hundredths).padStart(2, "0")}`;
  };

  const formatDuration = (durationMs) => formatClock(Number(durationMs) || 0);

  const formatDateTime = (iso) => {
    const dt = new Date(String(iso || ""));
    if (Number.isNaN(dt.getTime())) {
      return "-";
    }
    return dt.toLocaleString();
  };

  const elapsedMs = () => {
    if (!Number.isFinite(startedAtMs)) {
      return 0;
    }
    return Math.max(0, Date.now() - Number(startedAtMs));
  };

  const computeAps = (durationMs, moveCount) => {
    if (moveCount <= 0) {
      return 0;
    }
    const seconds = Math.max(durationMs / 1000, 0.001);
    return moveCount / seconds;
  };

  const computeScore = (durationMs, moveCount, aps) => {
    if (moveCount <= 0) {
      return 0;
    }
    const seconds = Math.max(durationMs / 1000, 0.001);
    const speedComponent = 90000 / (seconds + 20);
    const actionComponent = 75000 / (moveCount + 20);
    const apsComponent = Math.max(0, aps) * 450;
    return Math.max(0, Math.round(speedComponent + actionComponent + apsComponent));
  };

  const blankIndex = () => board.indexOf(0);

  const adjacentIndices = (index) => {
    const row = Math.floor(index / boardSize);
    const col = index % boardSize;
    const output = [];
    if (row > 0) output.push(index - boardSize);
    if (row < boardSize - 1) output.push(index + boardSize);
    if (col > 0) output.push(index - 1);
    if (col < boardSize - 1) output.push(index + 1);
    return output;
  };

  const tileIndexForKey = (deltaRow, deltaCol) => {
    const blank = blankIndex();
    const row = Math.floor(blank / boardSize);
    const col = blank % boardSize;
    const targetRow = row + deltaRow;
    const targetCol = col + deltaCol;
    if (targetRow < 0 || targetRow >= boardSize || targetCol < 0 || targetCol >= boardSize) {
      return null;
    }
    return (targetRow * boardSize) + targetCol;
  };

  const swap = (a, b) => {
    const tmp = board[a];
    board[a] = board[b];
    board[b] = tmp;
  };

  const moveTowardBlank = (tileIndex) => {
    const blank = blankIndex();
    if (tileIndex === blank) {
      return 0;
    }

    const blankRow = Math.floor(blank / boardSize);
    const blankCol = blank % boardSize;
    const tileRow = Math.floor(tileIndex / boardSize);
    const tileCol = tileIndex % boardSize;

    let delta = 0;
    if (tileRow === blankRow) {
      delta = tileIndex < blank ? -1 : 1;
    } else if (tileCol === blankCol) {
      delta = tileIndex < blank ? -boardSize : boardSize;
    } else {
      return 0;
    }

    let moved = 0;
    let currentBlank = blank;
    while (currentBlank !== tileIndex) {
      const next = currentBlank + delta;
      swap(currentBlank, next);
      currentBlank = next;
      moved += 1;
    }
    return moved;
  };

  const updateStats = () => {
    const duration = elapsedMs();
    const aps = computeAps(duration, actions);
    const score = computeScore(duration, actions, aps);
    clockEl.textContent = formatClock(duration);
    actionsEl.textContent = String(actions);
    apsEl.textContent = aps.toFixed(2);
    scoreEl.textContent = String(score);
    bestEl.textContent = String(bestScore);
  };

  const renderBoard = () => {
    const blank = blankIndex();
    const movable = new Set(adjacentIndices(blank));
    const frag = document.createDocumentFragment();

    board.forEach((value, idx) => {
      const tile = document.createElement("button");
      tile.type = "button";
      tile.className = "slide15-tile";
      tile.setAttribute("role", "gridcell");
      tile.dataset.tileIndex = String(idx);

      if (value === 0) {
        tile.classList.add("is-empty");
        tile.disabled = true;
        tile.setAttribute("aria-label", "Empty slot");
      } else {
        tile.textContent = String(value);
        tile.setAttribute("aria-label", `Tile ${value}`);
        if (movable.has(idx) && !solved) {
          tile.classList.add("is-movable");
        }
      }
      frag.appendChild(tile);
    });

    boardEl.replaceChildren(frag);
  };

  const renderHistory = () => {
    if (!Array.isArray(historyRows) || historyRows.length === 0) {
      historyBodyEl.innerHTML = "<tr><td colspan=\"7\" class=\"cell-data\">No recorded runs yet.</td></tr>";
      return;
    }

    const rows = historyRows.map((row) => {
      const state = String(row.state || "unknown").toLowerCase();
      const score = Number(row.score || 0);
      const stateClass = state === "solved" ? "badge-done" : "badge-excluded";
      const runId = String(row.id || "").trim();
      const manageCell = runId
        ? `<td class="cell-data slide15-col-manage"><button type="button" class="btn-mini btn-danger slide15-history-delete" data-slide15-delete="${runId}">Delete</button></td>`
        : "<td class=\"cell-data slide15-col-manage\">-</td>";
      return [
        "<tr class=\"data-row\">",
        `<td class=\"cell-data\">${formatDateTime(row.finished_at || row.saved_at)}</td>`,
        `<td class=\"cell-data\"><span class=\"badge ${stateClass}\">${state}</span></td>`,
        `<td class=\"cell-data\">${formatDuration(row.duration_ms)}</td>`,
        `<td class=\"cell-data\">${Number(row.actions || 0)}</td>`,
        `<td class=\"cell-data\">${Number(row.actions_per_second || 0).toFixed(2)}</td>`,
        `<td class=\"cell-data\">${score}</td>`,
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
    const history = Array.isArray(payload.history) ? payload.history : [];
    historyRows = history;
    bestScore = Number(payload.best_score || 0);
    renderHistory();
    updateStats();
  };

  const loadHistory = async () => {
    if (!historyUrl) {
      historyRows = [];
      bestScore = 0;
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
      // History delete failures should not block gameplay.
    }
  };

  const postRun = async (state) => {
    if (!recordUrl) {
      return;
    }

    const finishedAt = nowIso();
    const duration = elapsedMs();
    const aps = computeAps(duration, actions);
    const score = computeScore(duration, actions, aps);
    const startedAtIso = Number.isFinite(startedAtMs)
      ? new Date(Number(startedAtMs)).toISOString()
      : finishedAt;

    const payload = {
      state,
      started_at: startedAtIso,
      finished_at: finishedAt,
      duration_ms: Math.max(0, Math.floor(duration)),
      actions: Math.max(0, actions),
      actions_per_second: Number(aps.toFixed(4)),
      score: Math.max(0, score),
      board_size: boardSize,
      scramble_depth: scrambleDepth,
      input_counts: {
        keyboard: keyboardActions,
        click: clickActions,
      },
      board_start: boardStart.slice(),
      board_end: board.slice(),
    };

    try {
      const response = await fetch(recordUrl, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
        credentials: "same-origin",
      });
      if (!response.ok) {
        throw new Error(`History save failed (${response.status})`);
      }
      const body = await response.json();
      applyHistoryPayload(body);
    } catch (_err) {
      // History failures should not block gameplay.
    }
  };

  const beginTicker = () => {
    if (ticker !== null) {
      window.clearInterval(ticker);
      ticker = null;
    }
    ticker = window.setInterval(() => {
      if (!solved) {
        updateStats();
      }
    }, 50);
  };

  const stopTicker = () => {
    if (ticker !== null) {
      window.clearInterval(ticker);
      ticker = null;
    }
  };

  const makeScrambledBoard = () => {
    const cells = solvedBoard.slice();
    let empty = cells.indexOf(0);
    let prevEmpty = -1;

    const swapCells = (a, b) => {
      const temp = cells[a];
      cells[a] = cells[b];
      cells[b] = temp;
    };

    for (let i = 0; i < scrambleDepth; i += 1) {
      const possible = adjacentIndices(empty);
      const filtered = possible.filter((idx) => idx !== prevEmpty);
      const candidates = filtered.length > 0 ? filtered : possible;
      const next = candidates[Math.floor(Math.random() * candidates.length)];
      swapCells(empty, next);
      prevEmpty = empty;
      empty = next;
    }

    if (isSolvedBoard(cells)) {
      return makeScrambledBoard();
    }
    return cells;
  };

  const resetAttempt = () => {
    stopTicker();
    startedAtMs = null;
    actions = 0;
    keyboardActions = 0;
    clickActions = 0;
    solved = false;
    statusEl.textContent = "Ready (timer starts on first move)";

    renderBoard();
    updateStats();
  };

  const startNewPuzzle = () => {
    board = makeScrambledBoard();
    boardStart = board.slice();
    resetAttempt();
  };

  const restartPuzzle = () => {
    board = boardStart.slice();
    resetAttempt();
  };

  const onSolved = async () => {
    solved = true;
    stopTicker();
    updateStats();
    statusEl.textContent = "Solved!";
    await postRun("solved");
  };

  const tryMove = async (tileIndex, inputType) => {
    if (solved) {
      return;
    }
    const moved = moveTowardBlank(tileIndex);
    if (moved <= 0) {
      return;
    }

    if (!Number.isFinite(startedAtMs)) {
      startedAtMs = Date.now();
      statusEl.textContent = "In progress";
      beginTicker();
    }

    actions += moved;
    if (inputType === "keyboard") {
      keyboardActions += moved;
    } else {
      clickActions += 1;
    }

    renderBoard();
    updateStats();

    if (isSolvedBoard(board)) {
      await onSolved();
    }
  };

  boardEl.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const tile = target.closest("[data-tile-index]");
    if (!tile || !(tile instanceof HTMLElement)) {
      return;
    }
    const tileIndex = Number(tile.dataset.tileIndex);
    if (!Number.isInteger(tileIndex) || tileIndex < 0) {
      return;
    }
    void tryMove(tileIndex, "click");
  });

  restartBtn.addEventListener("click", () => {
    restartPuzzle();
  });

  newBtn.addEventListener("click", () => {
    startNewPuzzle();
  });

  historyBodyEl.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const button = target.closest("[data-slide15-delete]");
    if (!button || !(button instanceof HTMLElement)) {
      return;
    }
    const runId = String(button.dataset.slide15Delete || "").trim();
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

    const delta = keyDelta[event.key];
    if (!delta) {
      return;
    }

    event.preventDefault();
    const [dr, dc] = delta;
    const tileIndex = tileIndexForKey(dr, dc);
    if (tileIndex === null) {
      return;
    }
    void tryMove(tileIndex, "keyboard");
  });

  const init = async () => {
    await loadHistory();
    startNewPuzzle();
  };

  void init();
})();
