(() => {
  const root = document.querySelector("[data-bflip-app]");
  if (!root) {
    return;
  }

  const levelEl = root.querySelector("[data-bflip-level]");
  const roundScoreEl = root.querySelector("[data-bflip-round-score]");
  const totalScoreEl = root.querySelector("[data-bflip-total-score]");
  const highScoreEl = root.querySelector("[data-bflip-high-score]");
  const statusEl = root.querySelector("[data-bflip-status]");
  const newBtn = root.querySelector("[data-bflip-new]");
  const continueBtn = root.querySelector("[data-bflip-continue]");
  const spendBtn = root.querySelector("[data-bflip-spend]");
  const resetBtn = root.querySelector("[data-bflip-reset]");
  const boardEl = root.querySelector("[data-bflip-grid]");
  const boardWrapEl = root.querySelector(".bflip-board-wrap");
  const rowHintsEl = root.querySelector("[data-bflip-row-hints]");
  const colHintsEl = root.querySelector("[data-bflip-col-hints]");
  const historyBodyEl = root.querySelector("[data-bflip-history-body]");

  if (!levelEl || !roundScoreEl || !totalScoreEl || !highScoreEl || !statusEl || !newBtn || !continueBtn || !spendBtn || !resetBtn || !boardEl || !boardWrapEl || !rowHintsEl || !colHintsEl || !historyBodyEl) {
    return;
  }

  const historyUrl = String(root.dataset.historyUrl || "").trim();
  const recordUrl = String(root.dataset.recordUrl || historyUrl).trim();
  const deleteUrl = String(root.dataset.deleteUrl || historyUrl).trim();

  const BOARD_SIZE = 5;
  const CELL_COUNT = BOARD_SIZE * BOARD_SIZE;

  const LEVEL_CONFIGURATIONS = {
    1: [
      { twos: 3, threes: 1, bombs: 6 },
      { twos: 0, threes: 3, bombs: 6 },
      { twos: 5, threes: 0, bombs: 6 },
      { twos: 2, threes: 2, bombs: 6 },
      { twos: 4, threes: 1, bombs: 6 },
    ],
    2: [
      { twos: 1, threes: 3, bombs: 7 },
      { twos: 6, threes: 0, bombs: 7 },
      { twos: 3, threes: 2, bombs: 7 },
      { twos: 0, threes: 4, bombs: 7 },
      { twos: 5, threes: 1, bombs: 7 },
    ],
    3: [
      { twos: 2, threes: 3, bombs: 8 },
      { twos: 7, threes: 0, bombs: 8 },
      { twos: 4, threes: 2, bombs: 8 },
      { twos: 1, threes: 4, bombs: 8 },
      { twos: 6, threes: 1, bombs: 8 },
    ],
    4: [
      { twos: 3, threes: 3, bombs: 8 },
      { twos: 0, threes: 5, bombs: 8 },
      { twos: 8, threes: 0, bombs: 10 },
      { twos: 5, threes: 2, bombs: 10 },
      { twos: 2, threes: 4, bombs: 10 },
    ],
    5: [
      { twos: 7, threes: 1, bombs: 10 },
      { twos: 4, threes: 3, bombs: 10 },
      { twos: 1, threes: 5, bombs: 10 },
      { twos: 9, threes: 0, bombs: 10 },
      { twos: 6, threes: 2, bombs: 10 },
    ],
    6: [
      { twos: 3, threes: 4, bombs: 10 },
      { twos: 0, threes: 6, bombs: 10 },
      { twos: 8, threes: 1, bombs: 10 },
      { twos: 5, threes: 3, bombs: 10 },
      { twos: 2, threes: 5, bombs: 10 },
    ],
    7: [
      { twos: 7, threes: 2, bombs: 10 },
      { twos: 4, threes: 4, bombs: 10 },
      { twos: 1, threes: 6, bombs: 13 },
      { twos: 9, threes: 1, bombs: 13 },
      { twos: 6, threes: 3, bombs: 10 },
    ],
    8: [
      { twos: 0, threes: 7, bombs: 10 },
      { twos: 8, threes: 2, bombs: 10 },
      { twos: 5, threes: 4, bombs: 10 },
      { twos: 2, threes: 6, bombs: 10 },
      { twos: 7, threes: 3, bombs: 10 },
    ],
  };

  let level = 1;
  let pendingLevel = 1;
  let totalScore = 0;
  let highScore = 0;
  let roundScore = 1;
  let startedAtIso = null;
  let phase = "ready";
  let historyRows = [];
  let rowHints = [];
  let colHints = [];
  let board = [];
  let activeConfig = { twos: 0, threes: 0, bombs: 0 };
  let leftClicks = 0;
  let rightClicks = 0;
  let layoutSyncFrame = 0;

  const nowIso = () => new Date().toISOString();

  const pickOne = (items) => items[Math.floor(Math.random() * items.length)];

  const levelConfigs = (levelValue) => {
    const numeric = Number(levelValue);
    const clamped = Math.max(1, Math.min(8, Number.isFinite(numeric) ? Math.floor(numeric) : 1));
    return LEVEL_CONFIGURATIONS[clamped] || LEVEL_CONFIGURATIONS[8];
  };

  const shuffle = (values) => {
    for (let i = values.length - 1; i > 0; i -= 1) {
      const j = Math.floor(Math.random() * (i + 1));
      const tmp = values[i];
      values[i] = values[j];
      values[j] = tmp;
    }
    return values;
  };

  const buildBoardValues = (config) => {
    const values = [];
    for (let i = 0; i < config.bombs; i += 1) values.push(0);
    for (let i = 0; i < config.twos; i += 1) values.push(2);
    for (let i = 0; i < config.threes; i += 1) values.push(3);
    while (values.length < CELL_COUNT) values.push(1);
    return shuffle(values);
  };

  const calculateHints = () => {
    const rows = [];
    const cols = [];
    for (let row = 0; row < BOARD_SIZE; row += 1) {
      let rowPoints = 0;
      let rowBombs = 0;
      let colPoints = 0;
      let colBombs = 0;
      for (let col = 0; col < BOARD_SIZE; col += 1) {
        const rowCell = board[(row * BOARD_SIZE) + col];
        const colCell = board[(col * BOARD_SIZE) + row];

        if (rowCell.value === 0) rowBombs += 1;
        else rowPoints += rowCell.value;

        if (colCell.value === 0) colBombs += 1;
        else colPoints += colCell.value;
      }
      rows.push([rowPoints, rowBombs]);
      cols.push([colPoints, colBombs]);
    }
    rowHints = rows;
    colHints = cols;
  };

  const revealAll = () => {
    for (const cell of board) {
      cell.revealed = true;
    }
  };

  const checkRoundCleared = () => board.every((cell) => cell.value <= 1 || cell.revealed);

  const calculateLevelDecrease = (currentLevel) => {
    const baseChance = 0.1;
    const perLevelIncrease = 0.05;
    const totalChance = Math.min(0.9, baseChance + (Math.max(1, currentLevel) - 1) * perLevelIncrease);

    if (Math.random() >= totalChance) {
      return {
        level: currentLevel,
        text: "Level unchanged",
      };
    }

    const roll = Math.random();
    if (roll < 0.7) {
      return {
        level: Math.max(1, currentLevel - 1),
        text: "Level decreased by 1",
      };
    }
    if (roll < 0.9) {
      return {
        level: Math.max(1, currentLevel - 2),
        text: "Level decreased by 2",
      };
    }
    return {
      level: 1,
      text: "Critical drop to Level 1",
    };
  };

  const updateStats = () => {
    levelEl.textContent = String(level);
    roundScoreEl.textContent = String(roundScore);
    totalScoreEl.textContent = String(totalScore);
    highScoreEl.textContent = String(highScore);
  };

  const syncHintCellSize = () => {
    const firstCell = boardEl.querySelector(".bflip-cell");
    if (!(firstCell instanceof HTMLElement)) {
      return;
    }
    const rect = firstCell.getBoundingClientRect();
    const size = Math.min(rect.width, rect.height);
    if (!Number.isFinite(size) || size <= 0) {
      return;
    }
    boardWrapEl.style.setProperty("--bflip-cell-size", `${size}px`);
  };

  const queueHintCellSizeSync = () => {
    if (layoutSyncFrame) {
      window.cancelAnimationFrame(layoutSyncFrame);
    }
    layoutSyncFrame = window.requestAnimationFrame(() => {
      layoutSyncFrame = 0;
      syncHintCellSize();
    });
  };

  const renderHints = () => {
    rowHintsEl.innerHTML = rowHints
      .map((item, idx) => `<li><span>R${idx + 1}</span><strong>${item[0]}/${item[1]}</strong></li>`)
      .join("");

    colHintsEl.innerHTML = colHints
      .map((item, idx) => `<li><span>C${idx + 1}</span><strong>${item[0]}/${item[1]}</strong></li>`)
      .join("");
  };

  const renderBoard = () => {
    const frag = document.createDocumentFragment();
    board.forEach((cell, idx) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "bflip-cell";
      btn.dataset.bflipIndex = String(idx);
      btn.setAttribute("role", "gridcell");

      if (cell.revealed) {
        if (cell.value === 0) {
          btn.classList.add("is-bomb");
          btn.textContent = "B";
          btn.setAttribute("aria-label", "Bomb");
        } else {
          btn.classList.add("is-revealed");
          btn.textContent = String(cell.value);
          btn.setAttribute("aria-label", `Value ${cell.value}`);
        }
      } else if (cell.marked) {
        btn.classList.add("is-marked");
        btn.textContent = "!";
        btn.setAttribute("aria-label", "Marked bomb");
      } else {
        btn.textContent = "";
        btn.setAttribute("aria-label", "Hidden tile");
      }

      frag.appendChild(btn);
    });
    boardEl.replaceChildren(frag);
    queueHintCellSizeSync();
  };

  const renderHistory = () => {
    if (!Array.isArray(historyRows) || historyRows.length === 0) {
      historyBodyEl.innerHTML = "<tr><td colspan=\"6\" class=\"cell-data\">No recorded runs yet.</td></tr>";
      return;
    }

    const rows = historyRows.map((run) => {
      const state = String(run.state || "unknown").toLowerCase();
      const runId = String(run.id || "").trim();
      const stateClass = state === "cleared" ? "badge-done" : "badge-excluded";
      const finishedAt = new Date(String(run.finished_at || run.saved_at || ""));
      const when = Number.isNaN(finishedAt.getTime()) ? "-" : finishedAt.toLocaleString();
      const levelLabel = `${Number(run.level_start || 1)} -> ${Number(run.level_end || 1)}`;
      const manageCell = runId
        ? `<td class="cell-data bflip-col-manage"><button type="button" class="btn-mini btn-danger bflip-history-delete" data-bflip-delete="${runId}">Delete</button></td>`
        : "<td class=\"cell-data bflip-col-manage\">-</td>";
      return [
        "<tr class=\"data-row\">",
        `<td class="cell-data">${when}</td>`,
        `<td class="cell-data"><span class="badge ${stateClass}">${state}</span></td>`,
        `<td class="cell-data">${levelLabel}</td>`,
        `<td class="cell-data">${Number(run.round_score || 0)}</td>`,
        `<td class="cell-data">${Number(run.total_score_after || 0)}</td>`,
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
    highScore = Number(payload.best_total_score || 0);
    const nextLevel = Math.max(1, Number(payload.last_level || level));
    const nextTotal = Math.max(0, Number(payload.last_total_score || totalScore));
    if (phase !== "playing") {
      level = nextLevel;
      totalScore = nextTotal;
      pendingLevel = nextLevel;
    }
    updateStats();
    renderHistory();
  };

  const loadHistory = async () => {
    if (!historyUrl) {
      historyRows = [];
      highScore = 0;
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
      highScore = 0;
      renderHistory();
      updateStats();
    }
  };

  const postRun = async (state, levelEnd) => {
    if (!recordUrl || !startedAtIso) {
      return;
    }

    const payload = {
      state,
      started_at: startedAtIso,
      finished_at: nowIso(),
      level_start: level,
      level_end: Math.max(1, levelEnd),
      round_score: Math.max(1, roundScore),
      total_score_after: Math.max(0, totalScore),
      board_size: BOARD_SIZE,
      config: {
        twos: Number(activeConfig.twos || 0),
        threes: Number(activeConfig.threes || 0),
        bombs: Number(activeConfig.bombs || 0),
      },
      row_hints: rowHints,
      col_hints: colHints,
      board_revealed: board.map((cell) => Number(cell.value || 0)),
      input_counts: {
        left_click: leftClicks,
        right_click: rightClicks,
        keyboard: 0,
      },
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
      // History failures should not block gameplay.
    }
  };

  const startRound = () => {
    const config = pickOne(levelConfigs(level));
    activeConfig = {
      twos: Number(config.twos || 0),
      threes: Number(config.threes || 0),
      bombs: Number(config.bombs || 0),
    };
    const values = buildBoardValues(activeConfig);
    board = values.map((value) => ({
      value,
      revealed: false,
      marked: false,
    }));
    calculateHints();
    roundScore = 1;
    startedAtIso = nowIso();
    phase = "playing";
    pendingLevel = level;
    leftClicks = 0;
    rightClicks = 0;
    statusEl.textContent = "Round in progress";
    renderHints();
    renderBoard();
    updateStats();
  };

  const continueAfterRound = () => {
    if (phase !== "won" && phase !== "lost") {
      return;
    }
    level = pendingLevel;
    startRound();
  };

  const revealCell = async (index) => {
    if (phase !== "playing") {
      return;
    }
    const idx = Number(index);
    if (!Number.isInteger(idx) || idx < 0 || idx >= board.length) {
      return;
    }

    const cell = board[idx];
    if (!cell || cell.revealed || cell.marked) {
      return;
    }

    cell.revealed = true;
    leftClicks += 1;

    if (cell.value === 0) {
      const drop = calculateLevelDecrease(level);
      pendingLevel = drop.level;
      phase = "lost";
      revealAll();
      statusEl.textContent = `Bomb flipped. ${drop.text}. Press Continue.`;
      renderBoard();
      updateStats();
      await postRun("bombed", pendingLevel);
      return;
    }

    if (cell.value > 1) {
      roundScore *= cell.value;
    }

    if (checkRoundCleared()) {
      phase = "won";
      totalScore += roundScore;
      if (totalScore > highScore) {
        highScore = totalScore;
      }
      pendingLevel = level + 1;
      revealAll();
      statusEl.textContent = `Round cleared. +${roundScore} points. Press Continue.`;
      renderBoard();
      updateStats();
      await postRun("cleared", pendingLevel);
      return;
    }

    renderBoard();
    updateStats();
  };

  const toggleMark = (index) => {
    if (phase !== "playing") {
      return;
    }
    const idx = Number(index);
    if (!Number.isInteger(idx) || idx < 0 || idx >= board.length) {
      return;
    }

    const cell = board[idx];
    if (!cell || cell.revealed) {
      return;
    }

    cell.marked = !cell.marked;
    rightClicks += 1;
    renderBoard();
  };

  newBtn.addEventListener("click", () => {
    startRound();
  });

  continueBtn.addEventListener("click", () => {
    continueAfterRound();
  });

  spendBtn.addEventListener("click", () => {
    if (totalScore < 100) {
      statusEl.textContent = "Need at least 100 total score to buy a level.";
      return;
    }
    totalScore -= 100;
    level += 1;
    statusEl.textContent = "Bought +1 level and started a new round.";
    startRound();
  });

  resetBtn.addEventListener("click", () => {
    totalScore = 0;
    statusEl.textContent = "Total score reset.";
    updateStats();
  });

  boardEl.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const cell = target.closest("[data-bflip-index]");
    if (!cell || !(cell instanceof HTMLElement)) {
      return;
    }
    void revealCell(cell.dataset.bflipIndex);
  });

  boardEl.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const cell = target.closest("[data-bflip-index]");
    if (!cell || !(cell instanceof HTMLElement)) {
      return;
    }
    toggleMark(cell.dataset.bflipIndex);
  });

  historyBodyEl.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const button = target.closest("[data-bflip-delete]");
    if (!button || !(button instanceof HTMLElement)) {
      return;
    }
    const runId = String(button.getAttribute("data-bflip-delete") || "").trim();
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
    if (event.key === "Enter" && (phase === "won" || phase === "lost")) {
      event.preventDefault();
      continueAfterRound();
      return;
    }
    if (event.key === "n" || event.key === "N") {
      event.preventDefault();
      startRound();
    }
  });

  window.addEventListener("resize", () => {
    queueHintCellSizeSync();
  });

  const init = async () => {
    await loadHistory();
    statusEl.textContent = "Ready. Start a round.";
    startRound();
  };

  void init();
})();
