(() => {
  const root = document.querySelector("[data-snake-app]");
  if (!root) {
    return;
  }

  const canvasEl = root.querySelector("[data-snake-canvas]");
  const scoreEl = root.querySelector("[data-snake-score]");
  const lengthEl = root.querySelector("[data-snake-length]");
  const applesEl = root.querySelector("[data-snake-apples]");
  const speedEl = root.querySelector("[data-snake-speed]");
  const bestScoreEl = root.querySelector("[data-snake-best-score]");
  const statusEl = root.querySelector("[data-snake-status]");
  const newBtn = root.querySelector("[data-snake-new]");
  const pauseBtn = root.querySelector("[data-snake-pause]");
  const resumeBtn = root.querySelector("[data-snake-resume]");
  const speedSettingEl = root.querySelector("[data-snake-speed-setting]");
  const historyBodyEl = root.querySelector("[data-snake-history-body]");
  const directionButtons = Array.from(root.querySelectorAll("[data-snake-dir]"));

  if (!canvasEl || !(canvasEl instanceof HTMLCanvasElement) || !scoreEl || !lengthEl || !applesEl || !speedEl || !bestScoreEl || !statusEl || !newBtn || !pauseBtn || !resumeBtn || !speedSettingEl || !(speedSettingEl instanceof HTMLSelectElement) || !historyBodyEl || directionButtons.length !== 4) {
    return;
  }

  const ctx2d = canvasEl.getContext("2d");
  if (!ctx2d) {
    return;
  }

  const historyUrl = String(root.dataset.historyUrl || "").trim();
  const recordUrl = String(root.dataset.recordUrl || historyUrl).trim();
  const deleteUrl = String(root.dataset.deleteUrl || historyUrl).trim();
  const characterId = Math.max(0, Number(root.dataset.characterId || 0));

  const GRID_WIDTH = 20;
  const GRID_HEIGHT = 20;
  const TOTAL_CELLS = GRID_WIDTH * GRID_HEIGHT;
  const DEFAULT_SPEED_PRESET_KEY = "medium";
  const MAX_QUEUE = 3;

  const SPEED_PRESETS = {
    easy: {
      label: "Easy",
      startTps: 4.2,
      appleRamp: 0.22,
      stepRampEvery: 32,
      stepRampTps: 0.03,
      maxTps: 10.2,
    },
    medium: {
      label: "Medium",
      startTps: 5.4,
      appleRamp: 0.28,
      stepRampEvery: 30,
      stepRampTps: 0.04,
      maxTps: 12.4,
    },
    hard: {
      label: "Hard",
      startTps: 6.6,
      appleRamp: 0.34,
      stepRampEvery: 28,
      stepRampTps: 0.05,
      maxTps: 14.4,
    },
    "very-hard": {
      label: "Very Hard",
      startTps: 7.8,
      appleRamp: 0.4,
      stepRampEvery: 26,
      stepRampTps: 0.06,
      maxTps: 16.2,
    },
  };

  const DIRECTION_VECTORS = {
    up: { dx: 0, dy: -1 },
    down: { dx: 0, dy: 1 },
    left: { dx: -1, dy: 0 },
    right: { dx: 1, dy: 0 },
  };

  const OPPOSITE_DIRECTION = {
    up: "down",
    down: "up",
    left: "right",
    right: "left",
  };

  const KEY_TO_DIRECTION = {
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

  const PHASE_READY = "ready";
  const PHASE_PLAYING = "playing";
  const PHASE_PAUSED = "paused";
  const PHASE_LOST = "lost";

  let phase = PHASE_READY;
  let snakeCells = [];
  let foodCell = -1;
  let direction = "right";
  let directionQueue = [];

  let score = 0;
  let apples = 0;
  let maxLength = 2;
  let ticks = 0;
  let steps = 0;
  let selectedSpeedPresetKey = DEFAULT_SPEED_PRESET_KEY;
  let activeSpeedPresetKey = DEFAULT_SPEED_PRESET_KEY;
  let speedPreset = SPEED_PRESETS[DEFAULT_SPEED_PRESET_KEY];
  let speedTps = speedPreset.startTps;
  let speedStartTps = speedPreset.startTps;
  let speedEndTps = speedPreset.startTps;

  let spawnSeed = 0;
  let spawnCount = 0;
  let rngState = 0;

  let keyboardTurns = 0;
  let buttonTurns = 0;

  let startedAtIso = "";
  let endedAtIso = "";
  let runSaved = false;

  let historyRows = [];
  let bestScore = 0;

  let frameHandle = 0;
  let lastFrameTs = 0;
  let accumulator = 0;

  let canvasCssSize = 480;
  let cellPx = canvasCssSize / GRID_WIDTH;

  const nowIso = () => new Date().toISOString();

  const resolveSpeedPresetKey = (rawKey) => {
    const key = String(rawKey || "").trim().toLowerCase();
    if (Object.prototype.hasOwnProperty.call(SPEED_PRESETS, key)) {
      return key;
    }
    return DEFAULT_SPEED_PRESET_KEY;
  };

  const toIndex = (x, y) => (y * GRID_WIDTH) + x;

  const toPoint = (index) => ({
    x: index % GRID_WIDTH,
    y: Math.floor(index / GRID_WIDTH),
  });

  const formatDuration = (durationMs) => {
    const totalSeconds = Math.max(0, Math.floor(Number(durationMs || 0) / 1000));
    const mins = Math.floor(totalSeconds / 60);
    const secs = totalSeconds % 60;
    return `${mins}:${String(secs).padStart(2, "0")}`;
  };

  const updateStats = () => {
    scoreEl.textContent = String(score);
    lengthEl.textContent = String(snakeCells.length || 0);
    applesEl.textContent = String(apples);
    speedEl.textContent = speedTps.toFixed(2);
    bestScoreEl.textContent = String(bestScore);
  };

  const setStatus = (text) => {
    statusEl.textContent = text;
  };

  const applyIdleSpeedPreset = () => {
    speedTps = speedPreset.startTps;
    speedStartTps = speedPreset.startTps;
    speedEndTps = speedPreset.startTps;
  };

  const applySpeedPresetSelection = (rawKey) => {
    selectedSpeedPresetKey = resolveSpeedPresetKey(rawKey);
    speedSettingEl.value = selectedSpeedPresetKey;

    if (phase === PHASE_PLAYING || phase === PHASE_PAUSED) {
      const selectedPreset = SPEED_PRESETS[selectedSpeedPresetKey];
      setStatus(`${selectedPreset.label} selected. Applies on next New Game.`);
      return;
    }

    activeSpeedPresetKey = selectedSpeedPresetKey;
    speedPreset = SPEED_PRESETS[activeSpeedPresetKey];
    applyIdleSpeedPreset();
    updateStats();

    if (phase === PHASE_READY) {
      setStatus(`Ready. Press direction to start (${speedPreset.label}).`);
    }
  };

  const isReverseTurn = (fromDirection, toDirection) => {
    return OPPOSITE_DIRECTION[fromDirection] === toDirection;
  };

  const queueDirection = (nextDirection, inputKind) => {
    const dir = String(nextDirection || "").trim().toLowerCase();
    if (!DIRECTION_VECTORS[dir] || (phase !== PHASE_PLAYING && phase !== PHASE_READY)) {
      return;
    }

    const referenceDirection = directionQueue.length > 0
      ? directionQueue[directionQueue.length - 1]
      : direction;

    if (isReverseTurn(referenceDirection, dir)) {
      return;
    }

    if (phase === PHASE_READY) {
      direction = dir;
      if (inputKind === "keyboard") {
        keyboardTurns += 1;
      } else {
        buttonTurns += 1;
      }
      phase = PHASE_PLAYING;
      startedAtIso = nowIso();
      accumulator = 0;
      lastFrameTs = 0;
      setStatus(`In progress (${speedPreset.label})`);
      return;
    }

    if (dir === referenceDirection) {
      return;
    }

    if (directionQueue.length >= MAX_QUEUE) {
      return;
    }

    directionQueue.push(dir);
    if (inputKind === "keyboard") {
      keyboardTurns += 1;
    } else {
      buttonTurns += 1;
    }
  };

  const applyQueuedDirection = () => {
    while (directionQueue.length > 0) {
      const candidate = String(directionQueue.shift() || "");
      if (!candidate || isReverseTurn(direction, candidate)) {
        continue;
      }
      direction = candidate;
      return;
    }
  };

  const rand = () => {
    rngState = ((1103515245 * rngState) + 12345) % 2147483648;
    return rngState / 2147483648;
  };

  const recomputeSpeed = () => {
    const rampFromApples = apples * speedPreset.appleRamp;
    const rampFromSteps = Math.floor(steps / speedPreset.stepRampEvery) * speedPreset.stepRampTps;
    speedTps = Math.min(speedPreset.maxTps, speedPreset.startTps + rampFromApples + rampFromSteps);
  };

  const spawnFood = () => {
    const occupied = new Set(snakeCells);
    const empties = [];
    for (let cell = 0; cell < TOTAL_CELLS; cell += 1) {
      if (!occupied.has(cell)) {
        empties.push(cell);
      }
    }

    if (empties.length <= 0) {
      foodCell = -1;
      return false;
    }

    const pick = Math.floor(rand() * empties.length);
    foodCell = empties[Math.max(0, Math.min(empties.length - 1, pick))];
    spawnCount += 1;
    return true;
  };

  const drawRoundedRect = (x, y, width, height, radius, fillStyle, strokeStyle) => {
    const r = Math.max(0, Math.min(radius, width / 2, height / 2));
    ctx2d.beginPath();
    ctx2d.moveTo(x + r, y);
    ctx2d.lineTo(x + width - r, y);
    ctx2d.quadraticCurveTo(x + width, y, x + width, y + r);
    ctx2d.lineTo(x + width, y + height - r);
    ctx2d.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
    ctx2d.lineTo(x + r, y + height);
    ctx2d.quadraticCurveTo(x, y + height, x, y + height - r);
    ctx2d.lineTo(x, y + r);
    ctx2d.quadraticCurveTo(x, y, x + r, y);
    ctx2d.closePath();
    ctx2d.fillStyle = fillStyle;
    ctx2d.fill();
    if (strokeStyle) {
      ctx2d.strokeStyle = strokeStyle;
      ctx2d.stroke();
    }
  };

  const render = () => {
    const size = canvasCssSize;
    const pad = Math.max(1, Math.floor(cellPx * 0.08));
    const segmentSize = Math.max(2, Math.floor(cellPx - (pad * 2)));

    ctx2d.clearRect(0, 0, size, size);

    const bgGradient = ctx2d.createLinearGradient(0, 0, size, size);
    bgGradient.addColorStop(0, "#1b2e4a");
    bgGradient.addColorStop(1, "#15263d");
    ctx2d.fillStyle = bgGradient;
    ctx2d.fillRect(0, 0, size, size);

    ctx2d.strokeStyle = "rgba(120, 156, 204, 0.22)";
    ctx2d.lineWidth = 1;
    for (let x = 0; x <= GRID_WIDTH; x += 1) {
      const lineX = Math.round(x * cellPx) + 0.5;
      ctx2d.beginPath();
      ctx2d.moveTo(lineX, 0);
      ctx2d.lineTo(lineX, size);
      ctx2d.stroke();
    }
    for (let y = 0; y <= GRID_HEIGHT; y += 1) {
      const lineY = Math.round(y * cellPx) + 0.5;
      ctx2d.beginPath();
      ctx2d.moveTo(0, lineY);
      ctx2d.lineTo(size, lineY);
      ctx2d.stroke();
    }

    if (foodCell >= 0) {
      const food = toPoint(foodCell);
      const centerX = (food.x * cellPx) + (cellPx / 2);
      const centerY = (food.y * cellPx) + (cellPx / 2);
      const radius = Math.max(3, Math.floor(cellPx * 0.27));
      ctx2d.beginPath();
      ctx2d.arc(centerX, centerY, radius, 0, Math.PI * 2);
      ctx2d.fillStyle = "#f4c766";
      ctx2d.fill();
      ctx2d.strokeStyle = "#8f6c24";
      ctx2d.stroke();
    }

    for (let i = snakeCells.length - 1; i >= 0; i -= 1) {
      const segment = toPoint(snakeCells[i]);
      const segX = (segment.x * cellPx) + pad;
      const segY = (segment.y * cellPx) + pad;
      const head = i === 0;
      const fillColor = head ? "#63d6b2" : "#3ca4d0";
      drawRoundedRect(
        segX,
        segY,
        segmentSize,
        segmentSize,
        Math.max(3, Math.floor(cellPx * 0.22)),
        fillColor,
        "rgba(255, 255, 255, 0.15)",
      );
    }

    if (phase === PHASE_PAUSED || phase === PHASE_LOST) {
      ctx2d.fillStyle = "rgba(4, 10, 18, 0.56)";
      ctx2d.fillRect(0, 0, size, size);
      ctx2d.fillStyle = "#e7eef9";
      ctx2d.textAlign = "center";
      ctx2d.textBaseline = "middle";
      ctx2d.font = "700 26px Segoe UI";
      ctx2d.fillText(phase === PHASE_PAUSED ? "PAUSED" : "GAME OVER", size / 2, size / 2 - 8);
      ctx2d.font = "500 14px Segoe UI";
      ctx2d.fillStyle = "#c2d0e8";
      if (phase === PHASE_LOST) {
        ctx2d.fillText("Press New Game to restart", size / 2, size / 2 + 18);
      }
    }
  };

  const updateCanvasScale = () => {
    const rect = canvasEl.getBoundingClientRect();
    const nextCssSize = Math.max(280, Math.floor(Math.min(rect.width || 480, 560)));
    canvasCssSize = nextCssSize;
    cellPx = canvasCssSize / GRID_WIDTH;

    const dpr = window.devicePixelRatio || 1;
    canvasEl.width = Math.floor(canvasCssSize * dpr);
    canvasEl.height = Math.floor(canvasCssSize * dpr);
    ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);

    render();
  };

  const applyHistoryPayload = (payload) => {
    if (!payload || typeof payload !== "object") {
      return;
    }
    historyRows = Array.isArray(payload.history) ? payload.history : [];
    bestScore = Number(payload.best_score || 0);
    updateStats();
    renderHistory();
  };

  const renderHistory = () => {
    if (!Array.isArray(historyRows) || historyRows.length <= 0) {
      historyBodyEl.innerHTML = "<tr><td colspan=\"7\" class=\"cell-data\">No recorded runs yet.</td></tr>";
      return;
    }

    const rows = historyRows.map((run) => {
      const runId = String(run.id || "").trim();
      const cause = String(run.cause || "unknown").toLowerCase();
      const causeClass = cause === "self" ? "badge-excluded" : "badge-done";
      const finishedAt = new Date(String(run.finished_at || run.saved_at || ""));
      const when = Number.isNaN(finishedAt.getTime()) ? "-" : finishedAt.toLocaleString();
      const manageCell = runId
        ? `<td class=\"cell-data snake-col-manage\"><button type=\"button\" class=\"btn-mini btn-danger snake-history-delete\" data-snake-delete=\"${runId}\">Delete</button></td>`
        : "<td class=\"cell-data snake-col-manage\">-</td>";

      return [
        "<tr class=\"data-row\">",
        `<td class=\"cell-data\">${when}</td>`,
        `<td class=\"cell-data\"><span class=\"badge ${causeClass}\">${cause}</span></td>`,
        `<td class=\"cell-data\">${Number(run.score || 0)}</td>`,
        `<td class=\"cell-data\">${Number(run.apples || 0)}</td>`,
        `<td class=\"cell-data\">${Number(run.max_length || 0)}</td>`,
        `<td class=\"cell-data\">${formatDuration(Number(run.duration_ms || 0))}</td>`,
        manageCell,
        "</tr>",
      ].join("");
    }).join("");

    historyBodyEl.innerHTML = rows;
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
      // History delete failures should not interrupt gameplay.
    }
  };

  const postRun = async (cause) => {
    if (!recordUrl || runSaved || steps <= 0) {
      return;
    }

    runSaved = true;
    endedAtIso = nowIso();
    const durationMs = Math.max(
      0,
      Math.floor(new Date(endedAtIso).getTime() - new Date(startedAtIso || endedAtIso).getTime()),
    );

    const payload = {
      state: "lost",
      cause,
      started_at: String(startedAtIso || endedAtIso),
      finished_at: endedAtIso,
      duration_ms: durationMs,
      grid_width: GRID_WIDTH,
      grid_height: GRID_HEIGHT,
      ticks,
      steps,
      apples,
      score,
      max_length: maxLength,
      spawn_seed: spawnSeed,
      spawn_count: spawnCount,
      speed_start_tps: speedStartTps,
      speed_end_tps: speedEndTps,
      final_direction: direction,
      input_counts: {
        keyboard: keyboardTurns,
        button: buttonTurns,
      },
      snake_cells: snakeCells.slice(),
      food_cell: foodCell,
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
      // History save failures should not interrupt gameplay.
    }
  };

  const endRun = async (cause) => {
    phase = PHASE_LOST;
    speedEndTps = speedTps;
    if (cause === "self") {
      setStatus("Crashed into yourself.");
    } else {
      setStatus("Crashed into wall.");
    }
    updateStats();
    render();
    await postRun(cause);
  };

  const nextHeadCell = () => {
    const head = toPoint(snakeCells[0]);
    const vector = DIRECTION_VECTORS[direction];
    const nx = head.x + vector.dx;
    const ny = head.y + vector.dy;
    if (nx < 0 || ny < 0 || nx >= GRID_WIDTH || ny >= GRID_HEIGHT) {
      return -1;
    }
    return toIndex(nx, ny);
  };

  const checkSelfCollision = (candidateCell, willGrow) => {
    const occupied = willGrow ? snakeCells : snakeCells.slice(0, -1);
    return occupied.includes(candidateCell);
  };

  const tick = async () => {
    if (phase !== PHASE_PLAYING) {
      return;
    }

    applyQueuedDirection();

    const headCell = nextHeadCell();
    if (headCell < 0) {
      await endRun("wall");
      return;
    }

    const willGrow = headCell === foodCell;
    if (checkSelfCollision(headCell, willGrow)) {
      await endRun("self");
      return;
    }

    snakeCells.unshift(headCell);
    if (willGrow) {
      apples += 1;
      score += 10 + Math.floor(speedTps);
      maxLength = Math.max(maxLength, snakeCells.length);
      spawnFood();
    } else {
      snakeCells.pop();
    }

    steps += 1;
    ticks += 1;
    recomputeSpeed();

    setStatus("In progress");
    updateStats();
  };

  const newSeed = () => {
    const now = Date.now() >>> 0;
    const char = (characterId >>> 0) || 1;
    return (now ^ (char * 2654435761)) >>> 0;
  };

  const newGame = () => {
    const startX = Math.floor(GRID_WIDTH / 2);
    const startY = Math.floor(GRID_HEIGHT / 2);
    const head = toIndex(startX, startY);
    const tail = toIndex(startX - 1, startY);

    snakeCells = [head, tail];
    direction = "right";
    directionQueue = [];
    foodCell = -1;

    score = 0;
    apples = 0;
    maxLength = snakeCells.length;
    ticks = 0;
    steps = 0;
    activeSpeedPresetKey = resolveSpeedPresetKey(selectedSpeedPresetKey);
    speedPreset = SPEED_PRESETS[activeSpeedPresetKey];
    speedSettingEl.value = activeSpeedPresetKey;
    applyIdleSpeedPreset();

    spawnSeed = newSeed();
    spawnCount = 0;
    rngState = (spawnSeed % 2147483648) || 1;

    keyboardTurns = 0;
    buttonTurns = 0;

    startedAtIso = "";
    endedAtIso = "";
    runSaved = false;

    phase = PHASE_READY;
    accumulator = 0;
    lastFrameTs = 0;

    spawnFood();
    updateStats();
    setStatus(`Ready. Press direction to start (${speedPreset.label}).`);
    render();
  };

  const pauseGame = () => {
    if (phase !== PHASE_PLAYING) {
      return;
    }
    phase = PHASE_PAUSED;
    setStatus("Paused");
    render();
  };

  const resumeGame = () => {
    if (phase !== PHASE_PAUSED) {
      return;
    }
    phase = PHASE_PLAYING;
    setStatus("In progress");
    lastFrameTs = 0;
  };

  const frame = async (timestamp) => {
    if (phase === PHASE_PLAYING) {
      if (!lastFrameTs) {
        lastFrameTs = timestamp;
      }
      const deltaSeconds = Math.min(0.25, Math.max(0, (timestamp - lastFrameTs) / 1000));
      lastFrameTs = timestamp;
      accumulator += deltaSeconds;

      const tickInterval = 1 / Math.max(1, speedTps);
      while (accumulator >= tickInterval && phase === PHASE_PLAYING) {
        accumulator -= tickInterval;
        // eslint-disable-next-line no-await-in-loop
        await tick();
      }
    }

    render();
    frameHandle = window.requestAnimationFrame((ts) => {
      void frame(ts);
    });
  };

  newBtn.addEventListener("click", () => {
    newGame();
  });

  pauseBtn.addEventListener("click", () => {
    pauseGame();
  });

  resumeBtn.addEventListener("click", () => {
    resumeGame();
  });

  speedSettingEl.addEventListener("change", () => {
    applySpeedPresetSelection(speedSettingEl.value);
  });

  for (const button of directionButtons) {
    button.addEventListener("click", () => {
      const dir = String(button.getAttribute("data-snake-dir") || "").toLowerCase();
      queueDirection(dir, "button");
    });
  }

  historyBodyEl.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const button = target.closest("[data-snake-delete]");
    if (!button || !(button instanceof HTMLElement)) {
      return;
    }
    const runId = String(button.getAttribute("data-snake-delete") || "").trim();
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
      const tagName = String(target.tagName || "").toLowerCase();
      if (tagName === "input" || tagName === "textarea" || target.isContentEditable) {
        return;
      }
    }

    if (event.key === " " || event.key === "Spacebar") {
      event.preventDefault();
      if (phase === PHASE_PLAYING) {
        pauseGame();
      } else if (phase === PHASE_PAUSED) {
        resumeGame();
      }
      return;
    }

    const directionFromKey = KEY_TO_DIRECTION[event.key];
    if (!directionFromKey) {
      return;
    }

    event.preventDefault();
    queueDirection(directionFromKey, "keyboard");
  });

  window.addEventListener("resize", () => {
    updateCanvasScale();
  });

  const init = async () => {
    speedSettingEl.value = selectedSpeedPresetKey;
    updateCanvasScale();
    await loadHistory();
    newGame();

    if (!frameHandle) {
      frameHandle = window.requestAnimationFrame((ts) => {
        void frame(ts);
      });
    }
  };

  void init();
})();
