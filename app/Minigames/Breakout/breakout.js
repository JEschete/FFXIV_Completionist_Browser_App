(() => {
  const root = document.querySelector("[data-breakout-app]");
  if (!root) {
    return;
  }

  const canvasEl = root.querySelector("[data-breakout-canvas]");
  const scoreEl = root.querySelector("[data-breakout-score]");
  const livesEl = root.querySelector("[data-breakout-lives]");
  const levelEl = root.querySelector("[data-breakout-level]");
  const comboEl = root.querySelector("[data-breakout-combo]");
  const bestScoreEl = root.querySelector("[data-breakout-best-score]");
  const statusEl = root.querySelector("[data-breakout-status]");
  const newBtn = root.querySelector("[data-breakout-new]");
  const pauseBtn = root.querySelector("[data-breakout-pause]");
  const resumeBtn = root.querySelector("[data-breakout-resume]");
  const launchBtn = root.querySelector("[data-breakout-launch]");
  const historyBodyEl = root.querySelector("[data-breakout-history-body]");
  const moveButtons = Array.from(root.querySelectorAll("[data-breakout-move]"));

  if (!canvasEl || !(canvasEl instanceof HTMLCanvasElement) || !scoreEl || !livesEl || !levelEl || !comboEl || !bestScoreEl || !statusEl || !newBtn || !pauseBtn || !resumeBtn || !launchBtn || !historyBodyEl || moveButtons.length !== 2) {
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

  const BOARD_WIDTH = 900;
  const BOARD_HEIGHT = 520;
  const BRICK_ROWS = 7;
  const BRICK_COLS = 10;
  const MAX_LEVEL = 5;

  const PADDLE_BASE_WIDTH = 132;
  const PADDLE_HEIGHT = 16;
  const PADDLE_SPEED = 620;

  const BALL_RADIUS = 8;
  const BALL_BASE_SPEED = 340;
  const BALL_LEVEL_SPEED_BONUS = 18;
  const BALL_MAX_SPEED = 780;

  const POWERUP_DROP_CHANCE = 0.26;
  const EPSILON = 1e-7;

  const PHASE_READY = "ready";
  const PHASE_PLAYING = "playing";
  const PHASE_PAUSED = "paused";
  const PHASE_WON = "won";
  const PHASE_LOST = "lost";

  const keyState = {
    left: false,
    right: false,
  };

  let pointerPaddleX = null;

  let phase = PHASE_READY;
  let level = 1;
  let lives = 3;
  let score = 0;
  let combo = 0;
  let maxCombo = 0;

  let bestScore = 0;
  let historyRows = [];

  let bricks = [];
  let bricksTotal = 0;
  let bricksBroken = 0;
  let balls = [];
  let powerups = [];

  let paddle = {
    x: 0,
    y: 0,
    w: PADDLE_BASE_WIDTH,
    h: PADDLE_HEIGHT,
    vx: 0,
  };

  let powerupTimers = {
    expand: 0,
    slow: 0,
  };

  let powerupsCollected = {
    expand: 0,
    slow: 0,
    multiball: 0,
    life: 0,
  };

  let startedAtIso = "";
  let runSaved = false;
  let runState = "lost";
  let runCause = "drain";

  let durationMs = 0;
  let paddleHits = 0;
  let wallBounces = 0;
  let ballsLost = 0;
  let keyboardInputs = 0;
  let buttonInputs = 0;

  let speedStartPps = BALL_BASE_SPEED;
  let speedEndPps = BALL_BASE_SPEED;

  let rngSeed = 1;
  let rngState = 1;

  let frameId = 0;
  let lastFrameTs = 0;

  const nowIso = () => new Date().toISOString();

  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

  const rectsOverlap = (a, b) => (
    a.x < (b.x + b.w) &&
    (a.x + a.w) > b.x &&
    a.y < (b.y + b.h) &&
    (a.y + a.h) > b.y
  );

  const formatDuration = (ms) => {
    const seconds = Math.max(0, Math.floor(Number(ms || 0) / 1000));
    const mins = Math.floor(seconds / 60);
    const rem = seconds % 60;
    return `${mins}:${String(rem).padStart(2, "0")}`;
  };

  const updateStats = () => {
    scoreEl.textContent = String(score);
    livesEl.textContent = String(lives);
    levelEl.textContent = String(level);
    comboEl.textContent = String(combo);
    bestScoreEl.textContent = String(bestScore);
  };

  const setStatus = (text) => {
    statusEl.textContent = text;
  };

  const random = () => {
    rngState = ((1664525 * rngState) + 1013904223) >>> 0;
    return rngState / 4294967296;
  };

  const reseedRng = () => {
    const now = Date.now() >>> 0;
    const charMix = ((characterId >>> 0) * 2654435761) >>> 0;
    rngSeed = (now ^ charMix ^ ((level & 0xffff) << 8)) >>> 0;
    rngState = rngSeed || 1;
  };

  const resetPaddle = () => {
    paddle.w = powerupTimers.expand > 0 ? Math.floor(PADDLE_BASE_WIDTH * 1.45) : PADDLE_BASE_WIDTH;
    paddle.h = PADDLE_HEIGHT;
    paddle.x = Math.floor((BOARD_WIDTH - paddle.w) / 2);
    paddle.y = BOARD_HEIGHT - 38;
    paddle.vx = 0;
  };

  const makeBrickLayout = () => {
    const marginX = 28;
    const topY = 64;
    const gap = 6;
    const brickW = Math.floor((BOARD_WIDTH - (marginX * 2) - (gap * (BRICK_COLS - 1))) / BRICK_COLS);
    const brickH = 24;

    const next = [];
    for (let row = 0; row < BRICK_ROWS; row += 1) {
      for (let col = 0; col < BRICK_COLS; col += 1) {
        const rowBonus = row <= 1 ? 2 : row <= 3 ? 1 : 0;
        const durability = clamp(1 + Math.floor((level - 1) / 2) + rowBonus, 1, 6);
        const x = marginX + (col * (brickW + gap));
        const y = topY + (row * (brickH + gap));
        next.push({
          x,
          y,
          w: brickW,
          h: brickH,
          row,
          col,
          durability,
          maxDurability: durability,
          alive: true,
        });
      }
    }

    bricks = next;
    bricksTotal = bricks.length;
  };

  const spawnBall = (angleHint) => {
    const launchAngle = Number.isFinite(angleHint)
      ? angleHint
      : ((Math.PI * (0.30 + (random() * 0.4))));
    const speed = getTargetBallSpeed();
    const vx = Math.cos(launchAngle) * speed;
    const vy = -Math.abs(Math.sin(launchAngle) * speed);

    balls.push({
      x: paddle.x + (paddle.w / 2),
      y: paddle.y - BALL_RADIUS - 2,
      vx,
      vy,
      r: BALL_RADIUS,
    });
  };

  const resetBallsForLife = () => {
    balls = [{
      x: paddle.x + (paddle.w / 2),
      y: paddle.y - BALL_RADIUS - 2,
      vx: 0,
      vy: 0,
      r: BALL_RADIUS,
    }];
  };

  const getTargetBallSpeed = () => {
    const levelBase = BALL_BASE_SPEED + ((level - 1) * BALL_LEVEL_SPEED_BONUS);
    const progressRamp = Math.min(120, bricksBroken * 0.9);
    const slowFactor = powerupTimers.slow > 0 ? 0.72 : 1;
    return clamp((levelBase + progressRamp) * slowFactor, 240, BALL_MAX_SPEED);
  };

  const lockReadyBallToPaddle = () => {
    if (balls.length <= 0) {
      resetBallsForLife();
    }
    const ball = balls[0];
    ball.x = paddle.x + (paddle.w / 2);
    ball.y = paddle.y - ball.r - 2;
    ball.vx = 0;
    ball.vy = 0;
    if (balls.length > 1) {
      balls = [ball];
    }
  };

  const launchReadyBall = () => {
    if (phase !== PHASE_READY) {
      return;
    }

    lockReadyBallToPaddle();
    const ball = balls[0];
    const angle = Math.PI * (0.30 + (random() * 0.4));
    const speed = getTargetBallSpeed();
    ball.vx = Math.cos(angle) * speed;
    ball.vy = -Math.abs(Math.sin(angle) * speed);
    normalizeBallVelocity(ball);

    if (!startedAtIso) {
      startedAtIso = nowIso();
    }

    phase = PHASE_PLAYING;
    lastFrameTs = 0;
    setStatus("In progress");
  };

  const normalizeBallVelocity = (ball) => {
    const speed = Math.hypot(ball.vx, ball.vy);
    if (speed <= EPSILON) {
      ball.vx = getTargetBallSpeed();
      ball.vy = -getTargetBallSpeed();
      return;
    }
    const target = getTargetBallSpeed();
    const scale = target / speed;
    ball.vx *= scale;
    ball.vy *= scale;
    speedEndPps = Math.max(speedEndPps, target);
  };

  const powerupTypeRoll = () => {
    const roll = random();
    if (roll < 0.36) return "expand";
    if (roll < 0.62) return "slow";
    if (roll < 0.84) return "multiball";
    return "life";
  };

  const spawnPowerup = (brick) => {
    if (random() > POWERUP_DROP_CHANCE) {
      return;
    }

    powerups.push({
      x: brick.x + (brick.w / 2) - 16,
      y: brick.y + (brick.h / 2) - 8,
      w: 32,
      h: 14,
      vy: 130 + (level * 12),
      type: powerupTypeRoll(),
    });
  };

  const applyPowerup = (type) => {
    if (type === "expand") {
      powerupTimers.expand = Math.max(powerupTimers.expand, 12);
      powerupsCollected.expand += 1;
      const center = paddle.x + (paddle.w / 2);
      paddle.w = Math.floor(PADDLE_BASE_WIDTH * 1.45);
      paddle.x = clamp(center - (paddle.w / 2), 0, BOARD_WIDTH - paddle.w);
      if (phase === PHASE_READY) {
        lockReadyBallToPaddle();
      }
      setStatus("Powerup: wider paddle");
      return;
    }
    if (type === "slow") {
      powerupTimers.slow = Math.max(powerupTimers.slow, 8);
      powerupsCollected.slow += 1;
      for (const ball of balls) {
        ball.vx *= 0.84;
        ball.vy *= 0.84;
        normalizeBallVelocity(ball);
      }
      setStatus("Powerup: slow ball");
      return;
    }
    if (type === "multiball") {
      powerupsCollected.multiball += 1;
      const additions = [];
      for (const ball of balls) {
        if ((balls.length + additions.length) >= 4) {
          break;
        }
        additions.push({
          x: ball.x,
          y: ball.y,
          vx: -ball.vx,
          vy: ball.vy,
          r: ball.r,
        });
      }
      balls.push(...additions);
      setStatus("Powerup: multiball");
      return;
    }
    if (type === "life") {
      powerupsCollected.life += 1;
      lives += 1;
      setStatus("Powerup: extra life");
      updateStats();
    }
  };

  const renderHistory = () => {
    if (!Array.isArray(historyRows) || historyRows.length <= 0) {
      historyBodyEl.innerHTML = "<tr><td colspan=\"8\" class=\"cell-data\">No recorded runs yet.</td></tr>";
      return;
    }

    const rows = historyRows.map((run) => {
      const runId = String(run.id || "").trim();
      const state = String(run.state || "lost").toLowerCase();
      const stateClass = state === "won" ? "badge-done" : "badge-excluded";
      const finishedAt = new Date(String(run.finished_at || run.saved_at || ""));
      const when = Number.isNaN(finishedAt.getTime()) ? "-" : finishedAt.toLocaleString();
      const powerups = run.powerups_collected && typeof run.powerups_collected === "object"
        ? (
          Number(run.powerups_collected.expand || 0)
          + Number(run.powerups_collected.slow || 0)
          + Number(run.powerups_collected.multiball || 0)
          + Number(run.powerups_collected.life || 0)
        )
        : 0;
      const manageCell = runId
        ? `<td class=\"cell-data breakout-col-manage\"><button type=\"button\" class=\"btn-mini btn-danger breakout-history-delete\" data-breakout-delete=\"${runId}\">Delete</button></td>`
        : "<td class=\"cell-data breakout-col-manage\">-</td>";

      return [
        "<tr class=\"data-row\">",
        `<td class=\"cell-data\">${when}</td>`,
        `<td class=\"cell-data\"><span class=\"badge ${stateClass}\">${state}</span></td>`,
        `<td class=\"cell-data\">${Number(run.level_reached || 1)}</td>`,
        `<td class=\"cell-data\">${Number(run.score || 0)}</td>`,
        `<td class=\"cell-data\">${Number(run.bricks_broken || 0)}/${Number(run.bricks_total || 0)}</td>`,
        `<td class=\"cell-data\">${powerups}</td>`,
        `<td class=\"cell-data\">${formatDuration(Number(run.duration_ms || 0))}</td>`,
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
    updateStats();
    renderHistory();
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
      // History deletion errors should not break gameplay.
    }
  };

  const serializeBrickDurability = () => bricks.map((brick) => (brick.alive ? brick.durability : 0));

  const postRun = async () => {
    if (!recordUrl || runSaved) {
      return;
    }
    runSaved = true;

    const finishedAtIso = nowIso();
    const measuredDuration = Math.max(
      durationMs,
      Math.max(0, Math.floor(new Date(finishedAtIso).getTime() - new Date(startedAtIso || finishedAtIso).getTime())),
    );

    const payload = {
      state: runState,
      cause: runCause,
      started_at: String(startedAtIso || finishedAtIso),
      finished_at: finishedAtIso,
      duration_ms: measuredDuration,
      board_width: BOARD_WIDTH,
      board_height: BOARD_HEIGHT,
      level_reached: level,
      score,
      bricks_total: bricksTotal,
      bricks_broken: bricksBroken,
      balls_lost: ballsLost,
      max_combo: maxCombo,
      paddle_hits: paddleHits,
      wall_bounces: wallBounces,
      speed_start_pps: speedStartPps,
      speed_end_pps: Math.max(speedEndPps, getTargetBallSpeed()),
      brick_rows: BRICK_ROWS,
      brick_cols: BRICK_COLS,
      brick_durability_end: serializeBrickDurability(),
      input_counts: {
        keyboard: keyboardInputs,
        button: buttonInputs,
      },
      powerups_collected: {
        expand: powerupsCollected.expand,
        slow: powerupsCollected.slow,
        multiball: powerupsCollected.multiball,
        life: powerupsCollected.life,
      },
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

  const drawRoundedRect = (x, y, w, h, r, fillStyle, strokeStyle) => {
    const radius = clamp(r, 0, Math.min(w, h) / 2);
    ctx2d.beginPath();
    ctx2d.moveTo(x + radius, y);
    ctx2d.lineTo(x + w - radius, y);
    ctx2d.quadraticCurveTo(x + w, y, x + w, y + radius);
    ctx2d.lineTo(x + w, y + h - radius);
    ctx2d.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
    ctx2d.lineTo(x + radius, y + h);
    ctx2d.quadraticCurveTo(x, y + h, x, y + h - radius);
    ctx2d.lineTo(x, y + radius);
    ctx2d.quadraticCurveTo(x, y, x + radius, y);
    ctx2d.closePath();
    ctx2d.fillStyle = fillStyle;
    ctx2d.fill();
    if (strokeStyle) {
      ctx2d.strokeStyle = strokeStyle;
      ctx2d.stroke();
    }
  };

  const brickColor = (durability) => {
    if (durability >= 5) return "#c1644c";
    if (durability === 4) return "#be7d47";
    if (durability === 3) return "#97a84c";
    if (durability === 2) return "#4e9e8d";
    return "#4d86bf";
  };

  const powerupColor = (type) => {
    if (type === "expand") return "#5aa0e6";
    if (type === "slow") return "#63c19a";
    if (type === "multiball") return "#c78fe2";
    return "#e3b563";
  };

  const render = () => {
    ctx2d.clearRect(0, 0, BOARD_WIDTH, BOARD_HEIGHT);

    const bg = ctx2d.createLinearGradient(0, 0, BOARD_WIDTH, BOARD_HEIGHT);
    bg.addColorStop(0, "#14263d");
    bg.addColorStop(1, "#0f1f35");
    ctx2d.fillStyle = bg;
    ctx2d.fillRect(0, 0, BOARD_WIDTH, BOARD_HEIGHT);

    ctx2d.strokeStyle = "rgba(132, 167, 220, 0.1)";
    ctx2d.lineWidth = 1;
    for (let x = 0; x <= BOARD_WIDTH; x += 30) {
      ctx2d.beginPath();
      ctx2d.moveTo(x + 0.5, 0);
      ctx2d.lineTo(x + 0.5, BOARD_HEIGHT);
      ctx2d.stroke();
    }
    for (let y = 0; y <= BOARD_HEIGHT; y += 30) {
      ctx2d.beginPath();
      ctx2d.moveTo(0, y + 0.5);
      ctx2d.lineTo(BOARD_WIDTH, y + 0.5);
      ctx2d.stroke();
    }

    for (const brick of bricks) {
      if (!brick.alive) {
        continue;
      }
      drawRoundedRect(
        brick.x,
        brick.y,
        brick.w,
        brick.h,
        5,
        brickColor(brick.durability),
        "rgba(220, 236, 255, 0.28)",
      );
      if (brick.durability > 1) {
        ctx2d.fillStyle = "#ecf4ff";
        ctx2d.font = "700 12px Segoe UI";
        ctx2d.textAlign = "center";
        ctx2d.textBaseline = "middle";
        ctx2d.fillText(String(brick.durability), brick.x + (brick.w / 2), brick.y + (brick.h / 2));
      }
    }

    drawRoundedRect(
      paddle.x,
      paddle.y,
      paddle.w,
      paddle.h,
      7,
      "#79d6ba",
      "rgba(233, 250, 246, 0.45)",
    );

    for (const ball of balls) {
      ctx2d.beginPath();
      ctx2d.arc(ball.x, ball.y, ball.r, 0, Math.PI * 2);
      ctx2d.fillStyle = "#f4d179";
      ctx2d.fill();
      ctx2d.strokeStyle = "rgba(120, 83, 27, 0.6)";
      ctx2d.stroke();
    }

    for (const item of powerups) {
      drawRoundedRect(
        item.x,
        item.y,
        item.w,
        item.h,
        4,
        powerupColor(item.type),
        "rgba(236, 244, 255, 0.44)",
      );
      ctx2d.fillStyle = "#eff6ff";
      ctx2d.font = "700 10px Segoe UI";
      ctx2d.textAlign = "center";
      ctx2d.textBaseline = "middle";
      ctx2d.fillText(item.type.slice(0, 1).toUpperCase(), item.x + (item.w / 2), item.y + (item.h / 2));
    }

    if (phase === PHASE_PAUSED || phase === PHASE_WON || phase === PHASE_LOST) {
      ctx2d.fillStyle = "rgba(5, 11, 20, 0.58)";
      ctx2d.fillRect(0, 0, BOARD_WIDTH, BOARD_HEIGHT);

      ctx2d.textAlign = "center";
      ctx2d.textBaseline = "middle";
      ctx2d.font = "700 36px Segoe UI";
      ctx2d.fillStyle = "#edf4ff";
      const title = phase === PHASE_PAUSED ? "PAUSED" : phase === PHASE_WON ? "ALL LEVELS CLEARED" : "GAME OVER";
      ctx2d.fillText(title, BOARD_WIDTH / 2, BOARD_HEIGHT / 2 - 18);
      ctx2d.font = "500 16px Segoe UI";
      ctx2d.fillStyle = "#cddbef";
      if (phase !== PHASE_PAUSED) {
        ctx2d.fillText("Press New Game to play again", BOARD_WIDTH / 2, BOARD_HEIGHT / 2 + 16);
      }
    }
  };

  const updateCanvasScale = () => {
    const rect = canvasEl.getBoundingClientRect();
    const targetWidth = Math.max(320, Math.floor(Math.min(rect.width || BOARD_WIDTH, BOARD_WIDTH)));
    const targetHeight = Math.floor((targetWidth / BOARD_WIDTH) * BOARD_HEIGHT);

    canvasEl.style.height = `${targetHeight}px`;
    const dpr = window.devicePixelRatio || 1;
    canvasEl.width = Math.floor(BOARD_WIDTH * dpr);
    canvasEl.height = Math.floor(BOARD_HEIGHT * dpr);
    ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);

    render();
  };

  const updateTimers = (dt) => {
    const wasExpanded = powerupTimers.expand > 0;
    powerupTimers.expand = Math.max(0, powerupTimers.expand - dt);
    powerupTimers.slow = Math.max(0, powerupTimers.slow - dt);
    if (wasExpanded !== (powerupTimers.expand > 0)) {
      const center = paddle.x + (paddle.w / 2);
      paddle.w = powerupTimers.expand > 0 ? Math.floor(PADDLE_BASE_WIDTH * 1.45) : PADDLE_BASE_WIDTH;
      paddle.x = clamp(center - (paddle.w / 2), 0, BOARD_WIDTH - paddle.w);
    }
  };

  const updatePaddle = (dt) => {
    if (pointerPaddleX != null) {
      paddle.vx = 0;
      paddle.x = clamp(pointerPaddleX - (paddle.w / 2), 0, BOARD_WIDTH - paddle.w);
      return;
    }

    let dir = 0;
    if (keyState.left && !keyState.right) {
      dir = -1;
    } else if (keyState.right && !keyState.left) {
      dir = 1;
    }

    paddle.vx = dir * PADDLE_SPEED;
    paddle.x = clamp(paddle.x + (paddle.vx * dt), 0, BOARD_WIDTH - paddle.w);
  };

  const paddleRect = () => ({
    x: paddle.x,
    y: paddle.y,
    w: paddle.w,
    h: paddle.h,
  });

  const sweepPointAabb = (px, py, vx, vy, minX, minY, maxX, maxY, maxTime) => {
    let txEnter = -Infinity;
    let txExit = Infinity;
    if (Math.abs(vx) <= EPSILON) {
      if (px < minX || px > maxX) {
        return null;
      }
    } else {
      const tx1 = (minX - px) / vx;
      const tx2 = (maxX - px) / vx;
      txEnter = Math.min(tx1, tx2);
      txExit = Math.max(tx1, tx2);
    }

    let tyEnter = -Infinity;
    let tyExit = Infinity;
    if (Math.abs(vy) <= EPSILON) {
      if (py < minY || py > maxY) {
        return null;
      }
    } else {
      const ty1 = (minY - py) / vy;
      const ty2 = (maxY - py) / vy;
      tyEnter = Math.min(ty1, ty2);
      tyExit = Math.max(ty1, ty2);
    }

    let entry = Math.max(txEnter, tyEnter);
    const exit = Math.min(txExit, tyExit);

    if (entry > exit || exit < 0 || entry > maxTime) {
      return null;
    }
    if (entry < 0) {
      entry = 0;
    }

    let nx = 0;
    let ny = 0;
    if (txEnter > tyEnter) {
      nx = vx > 0 ? -1 : 1;
    } else {
      ny = vy > 0 ? -1 : 1;
    }

    return {
      t: entry,
      nx,
      ny,
    };
  };

  const sweepCircleRect = (ball, rect, maxTime) => {
    const minX = rect.x - ball.r;
    const minY = rect.y - ball.r;
    const maxX = rect.x + rect.w + ball.r;
    const maxY = rect.y + rect.h + ball.r;
    return sweepPointAabb(ball.x, ball.y, ball.vx, ball.vy, minX, minY, maxX, maxY, maxTime);
  };

  const breakBrick = (brick) => {
    if (!brick.alive) {
      return;
    }

    brick.durability -= 1;
    score += 6 * level;

    if (brick.durability <= 0) {
      brick.alive = false;
      bricksBroken += 1;
      combo += 1;
      maxCombo = Math.max(maxCombo, combo);
      score += (24 * level) + (combo * 3);
      spawnPowerup(brick);
    }
  };

  const updatePowerups = (dt) => {
    const next = [];
    const padRect = paddleRect();

    for (const item of powerups) {
      item.y += item.vy * dt;
      if ((item.y - item.h) > BOARD_HEIGHT) {
        continue;
      }
      if (rectsOverlap(item, padRect)) {
        applyPowerup(item.type);
        continue;
      }
      next.push(item);
    }

    powerups = next;
  };

  const runLostLife = async () => {
    combo = 0;
    lives -= 1;
    ballsLost += 1;

    if (lives <= 0) {
      lives = 0;
      phase = PHASE_LOST;
      runState = "lost";
      runCause = "drain";
      setStatus("Out of lives.");
      updateStats();
      await postRun();
      return;
    }

    phase = PHASE_READY;
    setStatus("Ball lost. Move paddle and press Up to launch.");
    resetPaddle();
    resetBallsForLife();
    updateStats();
  };

  const maybeAdvanceLevel = async () => {
    const remaining = bricks.some((brick) => brick.alive);
    if (remaining) {
      return;
    }

    score += 120 * level;
    combo = 0;

    if (level >= MAX_LEVEL) {
      phase = PHASE_WON;
      runState = "won";
      runCause = "cleared";
      setStatus("All levels cleared.");
      updateStats();
      await postRun();
      return;
    }

    level += 1;
    phase = PHASE_READY;
    setStatus(`Level ${level}. Press Up to launch.`);
    makeBrickLayout();
    powerups = [];
    resetPaddle();
    resetBallsForLife();
    updateStats();
  };

  const simulateBall = (ball, dt) => {
    let remaining = dt;
    let iterations = 0;

    while (remaining > EPSILON && iterations < 12) {
      iterations += 1;

      let best = null;

      const wallRects = [
        { kind: "left", x: -300, y: 0, w: 300, h: BOARD_HEIGHT },
        { kind: "right", x: BOARD_WIDTH, y: 0, w: 300, h: BOARD_HEIGHT },
        { kind: "top", x: 0, y: -300, w: BOARD_WIDTH, h: 300 },
      ];

      for (const wall of wallRects) {
        const hit = sweepCircleRect(ball, wall, remaining);
        if (!hit) {
          continue;
        }
        if (!best || hit.t < best.t) {
          best = {
            t: hit.t,
            nx: hit.nx,
            ny: hit.ny,
            kind: "wall",
            wall: wall.kind,
          };
        }
      }

      const padHit = sweepCircleRect(ball, paddleRect(), remaining);
      if (padHit && (!best || padHit.t < best.t)) {
        best = {
          t: padHit.t,
          nx: padHit.nx,
          ny: padHit.ny,
          kind: "paddle",
        };
      }

      for (const brick of bricks) {
        if (!brick.alive) {
          continue;
        }
        const hit = sweepCircleRect(ball, brick, remaining);
        if (!hit) {
          continue;
        }
        if (!best || hit.t < best.t) {
          best = {
            t: hit.t,
            nx: hit.nx,
            ny: hit.ny,
            kind: "brick",
            brick,
          };
        }
      }

      if (!best) {
        ball.x += ball.vx * remaining;
        ball.y += ball.vy * remaining;
        remaining = 0;
        break;
      }

      const travel = Math.max(0, best.t);
      ball.x += ball.vx * travel;
      ball.y += ball.vy * travel;
      remaining -= travel;

      if (best.kind === "wall") {
        wallBounces += 1;
        if (best.wall === "left" || best.wall === "right") {
          ball.vx = -ball.vx;
        } else {
          ball.vy = Math.abs(ball.vy);
        }
      } else if (best.kind === "paddle") {
        paddleHits += 1;
        combo = 0;
        const speed = Math.max(220, Math.hypot(ball.vx, ball.vy));
        const center = paddle.x + (paddle.w / 2);
        const offset = clamp((ball.x - center) / (paddle.w / 2), -1, 1);
        const angle = offset * (Math.PI * 0.38);
        ball.vx = (Math.sin(angle) * speed) + (paddle.vx * 0.22);
        ball.vy = -Math.abs(Math.cos(angle) * speed);
      } else if (best.kind === "brick") {
        breakBrick(best.brick);
        if (best.nx !== 0) {
          ball.vx = -ball.vx;
        }
        if (best.ny !== 0) {
          ball.vy = -ball.vy;
        }
      }

      normalizeBallVelocity(ball);

      if (remaining <= EPSILON) {
        break;
      }

      ball.x += best.nx * 0.05;
      ball.y += best.ny * 0.05;
      remaining = Math.max(0, remaining - 0.0005);
    }
  };

  const updateBalls = async (dt) => {
    for (const ball of balls) {
      simulateBall(ball, dt);
    }

    const kept = [];
    for (const ball of balls) {
      if ((ball.y - ball.r) > BOARD_HEIGHT + 2) {
        continue;
      }
      kept.push(ball);
    }

    if (kept.length !== balls.length) {
      balls = kept;
      if (balls.length === 0) {
        await runLostLife();
      }
    }

    await maybeAdvanceLevel();
  };

  const resetRunStats = () => {
    durationMs = 0;
    paddleHits = 0;
    wallBounces = 0;
    ballsLost = 0;
    keyboardInputs = 0;
    buttonInputs = 0;
    speedStartPps = BALL_BASE_SPEED;
    speedEndPps = BALL_BASE_SPEED;
    powerupsCollected = {
      expand: 0,
      slow: 0,
      multiball: 0,
      life: 0,
    };
  };

  const newGame = () => {
    phase = PHASE_READY;
    level = 1;
    lives = 3;
    score = 0;
    combo = 0;
    maxCombo = 0;
    bricksBroken = 0;
    runSaved = false;
    runState = "lost";
    runCause = "drain";
    startedAtIso = nowIso();

    powerupTimers.expand = 0;
    powerupTimers.slow = 0;
    powerups = [];

    resetRunStats();
    reseedRng();
    makeBrickLayout();
    resetPaddle();
    resetBallsForLife();

    updateStats();
    setStatus("Ready. Move paddle and press Up to launch.");
    render();
  };

  const pauseGame = () => {
    if (phase !== PHASE_PLAYING) {
      return;
    }
    phase = PHASE_PAUSED;
    setStatus("Paused");
  };

  const resumeGame = () => {
    if (phase !== PHASE_PAUSED) {
      return;
    }
    phase = PHASE_PLAYING;
    setStatus("In progress");
    lastFrameTs = 0;
  };

  const frame = async (ts) => {
    if (!lastFrameTs) {
      lastFrameTs = ts;
    }

    const dt = Math.min(0.033, Math.max(0, (ts - lastFrameTs) / 1000));
    lastFrameTs = ts;

    if (phase === PHASE_PLAYING) {
      durationMs += Math.floor(dt * 1000);
      updateTimers(dt);
      updatePaddle(dt);
      await updateBalls(dt);
      updatePowerups(dt);
      updateStats();
    } else if (phase === PHASE_READY) {
      updateTimers(dt);
      updatePaddle(dt);
      lockReadyBallToPaddle();
      updateStats();
    }

    render();

    frameId = window.requestAnimationFrame((nextTs) => {
      void frame(nextTs);
    });
  };

  const setMoveState = (side, isDown, source) => {
    if (side === "left") {
      if (isDown && !keyState.left && source === "button") {
        buttonInputs += 1;
      }
      keyState.left = isDown;
      return;
    }
    if (side === "right") {
      if (isDown && !keyState.right && source === "button") {
        buttonInputs += 1;
      }
      keyState.right = isDown;
    }
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

  launchBtn.addEventListener("click", () => {
    if (phase !== PHASE_READY) {
      return;
    }
    buttonInputs += 1;
    launchReadyBall();
  });

  for (const button of moveButtons) {
    const side = String(button.getAttribute("data-breakout-move") || "").toLowerCase();
    if (side !== "left" && side !== "right") {
      continue;
    }

    const down = (event) => {
      event.preventDefault();
      setMoveState(side, true, "button");
    };
    const up = (event) => {
      event.preventDefault();
      setMoveState(side, false, "button");
    };

    button.addEventListener("pointerdown", down);
    button.addEventListener("pointerup", up);
    button.addEventListener("pointercancel", up);
    button.addEventListener("pointerleave", up);
  }

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

    if (event.key === "ArrowUp") {
      if (phase === PHASE_READY) {
        event.preventDefault();
        keyboardInputs += 1;
        launchReadyBall();
      }
      return;
    }

    if (event.key === "ArrowLeft" || event.key === "a" || event.key === "A") {
      event.preventDefault();
      if (!event.repeat && !keyState.left) {
        keyboardInputs += 1;
      }
      keyState.left = true;
      return;
    }

    if (event.key === "ArrowRight" || event.key === "d" || event.key === "D") {
      event.preventDefault();
      if (!event.repeat && !keyState.right) {
        keyboardInputs += 1;
      }
      keyState.right = true;
    }
  });

  window.addEventListener("keyup", (event) => {
    if (event.key === "ArrowLeft" || event.key === "a" || event.key === "A") {
      keyState.left = false;
      return;
    }
    if (event.key === "ArrowRight" || event.key === "d" || event.key === "D") {
      keyState.right = false;
    }
  });

  const mapClientX = (clientX) => {
    const rect = canvasEl.getBoundingClientRect();
    if (!rect.width) {
      return null;
    }
    const normX = (clientX - rect.left) / rect.width;
    return clamp(normX * BOARD_WIDTH, 0, BOARD_WIDTH);
  };

  canvasEl.addEventListener("mousemove", (event) => {
    const mapped = mapClientX(event.clientX);
    if (mapped != null) {
      pointerPaddleX = mapped;
    }
  });

  canvasEl.addEventListener("mouseleave", () => {
    pointerPaddleX = null;
  });

  canvasEl.addEventListener("touchstart", (event) => {
    const touch = event.touches[0];
    if (!touch) {
      return;
    }
    const mapped = mapClientX(touch.clientX);
    if (mapped != null) {
      pointerPaddleX = mapped;
    }
  }, { passive: true });

  canvasEl.addEventListener("touchmove", (event) => {
    const touch = event.touches[0];
    if (!touch) {
      return;
    }
    const mapped = mapClientX(touch.clientX);
    if (mapped != null) {
      pointerPaddleX = mapped;
    }
  }, { passive: true });

  canvasEl.addEventListener("touchend", () => {
    pointerPaddleX = null;
  }, { passive: true });

  historyBodyEl.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const button = target.closest("[data-breakout-delete]");
    if (!button || !(button instanceof HTMLElement)) {
      return;
    }
    const runId = String(button.getAttribute("data-breakout-delete") || "").trim();
    if (!runId) {
      return;
    }
    if (!window.confirm("Delete this run from history?")) {
      return;
    }
    void deleteRun(runId);
  });

  window.addEventListener("resize", () => {
    updateCanvasScale();
  });

  const init = async () => {
    updateCanvasScale();
    await loadHistory();
    setStatus("Ready. Press New Game.");
    updateStats();
    newGame();

    if (!frameId) {
      frameId = window.requestAnimationFrame((ts) => {
        void frame(ts);
      });
    }
  };

  void init();
})();
