"use strict";

const state = {
  runs: [],
  stats: null,
  filter: "ALL",
  loading: false,
};

const elements = {
  runGrid: document.querySelector("#run-grid"),
  emptyState: document.querySelector("#empty-state"),
  refreshButton: document.querySelector("#refresh-button"),
  syncStatus: document.querySelector("#sync-status"),
  statCount: document.querySelector("#stat-count"),
  statAct4: document.querySelector("#stat-act4"),
  statHeart: document.querySelector("#stat-heart"),
  statDeepest: document.querySelector("#stat-deepest"),
  statFloor: document.querySelector("#stat-floor"),
  statActions: document.querySelector("#stat-actions"),
  statAverageFloor: document.querySelector("#stat-average-floor"),
  dialog: document.querySelector("#run-dialog"),
  dialogKicker: document.querySelector("#dialog-kicker"),
  dialogTitle: document.querySelector("#dialog-title"),
  dialogContent: document.querySelector("#dialog-content"),
  dialogClose: document.querySelector("#dialog-close"),
};

const characterLabels = {
  IRONCLAD: "铁甲战士",
  THE_SILENT: "静默猎手",
  DEFECT: "故障机器人",
  WATCHER: "观者",
};

const findingLabels = {
  high_impact_default_choice: "高影响默认选择",
  producer_consequence_claim_contradicted: "结果声明与独立证据冲突",
  high_value_resource_loss_choice: "高价值资源损失选择",
  controller_exit_failure: "控制器退出证据异常",
};

function escapeHTML(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatNumber(value, fallback = "—") {
  return Number.isFinite(Number(value))
    ? new Intl.NumberFormat("zh-CN").format(Number(value))
    : fallback;
}

function formatPercent(value) {
  if (value == null || value === "") return "—";
  const numeric = Number(value);
  return Number.isFinite(numeric)
    ? new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 1 }).format(numeric)
    : "—";
}

function formatCost(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  if (numeric === 0) return "$0";
  return `$${numeric.toFixed(numeric < 0.01 ? 4 : 3)}`;
}

function formatTimestamp(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value * 1000));
}

function formatFullTimestamp(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return "未知";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value * 1000));
}

function formatDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "未知";
  const minutes = Math.round(value / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours} 小时 ${remainder} 分` : `${hours} 小时`;
}

function resultInfo(run) {
  if (run.heart_defeated) {
    return {
      headline: "腐化之心已击败",
      badge: "心脏胜利",
      className: "badge-result-win",
    };
  }
  if (run.victory) {
    return {
      headline: "第三幕通关",
      badge: "通关",
      className: "badge-result-win",
    };
  }
  if (Number(run.observed_max_act) >= 4) {
    return {
      headline: `止步第四幕 · ${formatNumber(run.floor)} 层`,
      badge: "第四幕落败",
      className: "badge-result-loss",
    };
  }
  return {
    headline: `止步第${chineseAct(run.act)}幕 · ${formatNumber(run.floor)} 层`,
    badge: "本局落败",
    className: "badge-result-loss",
  };
}

function chineseAct(act) {
  return { 1: "一", 2: "二", 3: "三", 4: "四" }[Number(act)] ?? formatNumber(act);
}

function auditInfo(run) {
  if (run.controller_exit_status !== "clear") {
    return { label: "退出证据异常", className: "badge-issues" };
  }
  const status = run.audit?.status;
  if (status === "clear") {
    return { label: "审计通过", className: "badge-clear" };
  }
  if (status === "issues") {
    return { label: "审计有发现", className: "badge-issues" };
  }
  return { label: "审计未就绪", className: "badge-missing" };
}

function characterClass(character) {
  return `character-${String(character || "unknown")
    .toLowerCase()
    .replaceAll("_", "-")}`;
}

function keyGem(key, owned, label) {
  return `
    <span class="key-gem key-${escapeHTML(key)} ${owned ? "is-owned" : ""}"
      title="${escapeHTML(label)}${owned ? "已获得" : "未获得"}">
      <span>${escapeHTML(label.slice(0, 1))}</span>
    </span>`;
}

function renderKeys(keys) {
  return [
    keyGem("ruby", keys?.ruby, "红钥匙"),
    keyGem("emerald", keys?.emerald, "绿钥匙"),
    keyGem("sapphire", keys?.sapphire, "蓝钥匙"),
  ].join("");
}

function runCard(run) {
  const result = resultInfo(run);
  const audit = auditInfo(run);
  const model = run.audit?.model || {};
  const hp = run.current_hp == null || run.max_hp == null
    ? "—"
    : `${formatNumber(run.current_hp)}/${formatNumber(run.max_hp)}`;
  return `
    <article class="run-card ${characterClass(run.character)}">
      <div class="run-rank" aria-label="按完成时间第 ${escapeHTML(run.rank)} 局">${escapeHTML(
        String(run.rank).padStart(2, "0"),
      )}</div>
      <div class="run-main">
        <div class="run-topline">
          <span class="character-name">${escapeHTML(
            run.character_name || characterLabels[run.character] || run.character,
          )}</span>
          <span class="run-time">${escapeHTML(formatTimestamp(run.ended_at))}</span>
        </div>
        <h3>${escapeHTML(result.headline)}</h3>
        <div class="run-badges">
          <span class="badge ${result.className}">${escapeHTML(result.badge)}</span>
          <span class="badge ${audit.className}">${escapeHTML(audit.label)}</span>
          <span class="badge">A${escapeHTML(run.ascension_level ?? "?")}</span>
        </div>
        <div class="model-summary" aria-label="模型调用统计">
          <span><small>模型调用</small><strong>${escapeHTML(
            formatNumber(model.remote_consultations, "0"),
          )}</strong></span>
          <span><small>有效建议</small><strong>${escapeHTML(
            formatNumber(model.remote_valid_recommendations, "0"),
          )}</strong></span>
          <span class="model-adopted"><small>最终采纳</small><strong>${escapeHTML(
            formatNumber(model.semantic_choice_changes, "0"),
          )}</strong></span>
        </div>
        <div class="run-metrics">
          <span><strong>${escapeHTML(hp)}</strong> HP</span>
          <span><strong>${escapeHTML(run.deck_count)}</strong> 卡</span>
          <span><strong>${escapeHTML(run.relic_count)}</strong> 遗物</span>
          <span><strong>${escapeHTML(formatNumber(run.actions))}</strong> 行动</span>
        </div>
      </div>
      <div class="run-side">
        <div class="run-keys" aria-label="钥匙收集情况">${renderKeys(run.keys)}</div>
        <button class="detail-button" type="button" data-attempt-id="${escapeHTML(
          run.attempt_id,
        )}">查看详情</button>
      </div>
    </article>`;
}

function renderStats() {
  const stats = state.stats || {};
  elements.statCount.textContent = formatNumber(state.runs.length, "0");
  elements.statAct4.textContent = formatNumber(stats.act4_entries, "0");
  elements.statHeart.textContent = `腐化之心胜利 ${formatNumber(
    stats.heart_victories,
    "0",
  )} 局`;
  if (stats.deepest) {
    elements.statDeepest.textContent = `Act ${formatNumber(stats.deepest.act)}`;
    elements.statFloor.textContent = `最深到达 ${formatNumber(stats.deepest.floor)} 层`;
  } else {
    elements.statDeepest.textContent = "—";
    elements.statFloor.textContent = "等待记录";
  }
  elements.statActions.textContent = formatNumber(stats.average_actions, "0");
  elements.statAverageFloor.textContent = `平均楼层 ${formatNumber(
    stats.average_floor,
    "0",
  )}`;
}

function renderRuns() {
  const visible = state.filter === "ALL"
    ? state.runs
    : state.runs.filter((run) => run.character === state.filter);

  elements.runGrid.innerHTML = visible.map(runCard).join("");
  elements.runGrid.hidden = visible.length === 0;
  elements.emptyState.hidden = visible.length !== 0;

  elements.runGrid.querySelectorAll("[data-attempt-id]").forEach((button) => {
    button.addEventListener("click", () => openRunDetail(button.dataset.attemptId));
  });
}

function setSyncStatus(message, isError = false) {
  elements.syncStatus.textContent = message;
  elements.syncStatus.parentElement.classList.toggle("is-error", isError);
}

async function loadRuns({ quiet = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  elements.refreshButton.disabled = true;
  if (!quiet) setSyncStatus("正在读取完成归档");
  try {
    const response = await fetch("/api/runs", {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    state.runs = Array.isArray(payload.runs) ? payload.runs : [];
    state.stats = payload.stats || {};
    renderStats();
    renderRuns();
    setSyncStatus(`已同步 · ${formatTimestamp(payload.generated_at)}`);
  } catch (error) {
    if (!state.runs.length) {
      elements.runGrid.hidden = true;
      elements.emptyState.hidden = false;
      elements.emptyState.querySelector("h3").textContent = "暂时无法读取完成归档";
      elements.emptyState.querySelector("p").textContent = "本地服务仍在运行，可稍后手动刷新。";
    }
    setSyncStatus("读取失败，可手动重试", true);
  } finally {
    state.loading = false;
    elements.refreshButton.disabled = false;
  }
}

function badgeHTML(label, className = "") {
  return `<span class="badge ${escapeHTML(className)}">${escapeHTML(label)}</span>`;
}

function resourceChips(resources, type) {
  if (!Array.isArray(resources) || resources.length === 0) {
    return '<span class="resource-empty">无可展示记录</span>';
  }
  return resources
    .map((item) => {
      const upgrade = Number(item.upgrades) > 0 ? `+${item.upgrades}` : "";
      const count = Number(item.count) > 1 ? `×${item.count}` : "";
      const counter = type === "relic" && item.counter != null && Number(item.counter) >= 0
        ? `<small>计数 ${escapeHTML(item.counter)}</small>`
        : "";
      const meta = type === "card"
        ? `<small>${escapeHTML(item.type || "")}${item.cost == null ? "" : ` · ${escapeHTML(item.cost)}费`}</small>`
        : counter;
      return `<span class="resource-chip"><span>${escapeHTML(
        item.name || item.id,
      )}${escapeHTML(upgrade)}</span>${count ? `<strong>${escapeHTML(count)}</strong>` : ""}${meta}</span>`;
    })
    .join("");
}

function renderFinding(item) {
  const kind = findingLabels[item.kind] || String(item.kind || "未分类发现").replaceAll("_", " ");
  const location = [
    item.act != null ? `Act ${item.act}` : null,
    item.floor != null ? `${item.floor} 层` : null,
    item.turn != null ? `回合 ${item.turn}` : null,
    item.phase || null,
  ].filter(Boolean);
  return `
    <article class="finding-item">
      <strong>${escapeHTML(kind)}</strong>
      <div class="finding-meta">
        ${item.severity ? `<span>${escapeHTML(item.severity)}</span>` : ""}
        ${location.map((value) => `<span>${escapeHTML(value)}</span>`).join("")}
        ${item.reason ? `<span>原因：${escapeHTML(item.reason)}</span>` : ""}
      </div>
    </article>`;
}

function replayActionHTML(action) {
  const player = action.player_before || {};
  const outcome = action.outcome || {};
  const selected = action.selected_name || (action.kind === "end" ? "" : action.selected_id);
  return `
    <div class="replay-action">
      <div class="replay-action-name">
        ${escapeHTML(action.label || action.kind)}${selected ? ` · ${escapeHTML(selected)}` : ""}
        <small>状态序列 ${escapeHTML(action.before_seq ?? "—")} → ${escapeHTML(
          action.after_seq ?? "—",
        )}</small>
      </div>
      <span class="replay-value">HP <strong>${escapeHTML(player.hp ?? "—")}</strong></span>
      <span class="replay-value">格挡 <strong>${escapeHTML(player.block ?? "—")}</strong></span>
      <span class="replay-value">伤害 <strong>${escapeHTML(outcome.enemy_hp_loss ?? 0)}</strong> / 承伤 <strong>${escapeHTML(
        outcome.player_hp_loss ?? 0,
      )}</strong></span>
    </div>`;
}

function replayTurnHTML(turn) {
  const player = turn.player_at_start || {};
  const monsters = Array.isArray(turn.monsters) ? turn.monsters : [];
  const monsterText = monsters.length
    ? monsters
        .map((monster) => `${monster.name || monster.id} ${monster.hp ?? "?"}/${monster.max_hp ?? "?"}`)
        .join(" · ")
    : "敌人信息未记录";
  return `
    <article class="replay-turn">
      <header class="replay-turn-header">
        <strong>回合 ${escapeHTML(turn.turn ?? "—")} · HP ${escapeHTML(
          player.hp ?? "—",
        )}/${escapeHTML(player.max_hp ?? "—")}</strong>
        <span>${escapeHTML(monsterText)}</span>
      </header>
      <div class="replay-actions">
        ${(turn.actions || []).map(replayActionHTML).join("") || '<div class="replay-action">没有可展示行动</div>'}
      </div>
    </article>`;
}

function renderDetail(payload) {
  const run = payload.run;
  const result = resultInfo(run);
  const audit = auditInfo(run);
  const build = payload.build || {};
  const auditDetail = payload.audit_detail || {};
  const issues = Array.isArray(auditDetail.issues) ? auditDetail.issues : [];
  const findings = Array.isArray(auditDetail.review_findings)
    ? auditDetail.review_findings
    : [];
  const replay = payload.death_replay;
  const auditCount = run.audit?.issue_count;
  const unknownCount = run.audit?.eligible_unknown_count;
  const model = run.audit?.model || {};

  elements.dialogKicker.textContent = `${run.character_name || run.character} · ${formatFullTimestamp(
    run.ended_at,
  )}`;
  elements.dialogTitle.textContent = result.headline;

  const replaySection = replay?.turns?.length
    ? `
      <section class="detail-section">
        <div class="detail-section-heading">
          <h3>终局三回合</h3>
          <span>${escapeHTML(replay.status)} · ${escapeHTML(replay.replay_kind)}</span>
        </div>
        <div class="replay-grid">${replay.turns.map(replayTurnHTML).join("")}</div>
      </section>`
    : "";

  const findingSection = issues.length || findings.length
    ? `
      <section class="detail-section">
        <div class="detail-section-heading">
          <h3>审计摘要</h3>
          <span>仅展示前 8 条，游戏结果不受此状态影响</span>
        </div>
        <div class="finding-list">
          ${issues.map(renderFinding).join("")}
          ${findings.map(renderFinding).join("")}
        </div>
      </section>`
    : "";

  elements.dialogContent.innerHTML = `
    <section class="detail-hero">
      <article class="detail-panel">
        <p class="detail-result">${escapeHTML(result.headline)}</p>
        <p class="detail-subtitle">种子 ${escapeHTML(run.seed)} · 用时 ${escapeHTML(
          formatDuration(run.duration_seconds),
        )}</p>
        <div class="detail-badges">
          ${badgeHTML(result.badge, result.className)}
          ${badgeHTML(audit.label, audit.className)}
          ${badgeHTML(`A${run.ascension_level ?? "?"}`)}
          ${badgeHTML(`行动 ${formatNumber(run.actions)}`)}
        </div>
      </article>
      <article class="detail-panel">
        <div class="detail-facts">
          <div class="detail-fact"><span>终局位置</span><strong>Act ${escapeHTML(
            run.act,
          )} · ${escapeHTML(run.floor)} 层</strong></div>
          <div class="detail-fact"><span>最终生命</span><strong>${escapeHTML(
            run.current_hp ?? "—",
          )}/${escapeHTML(run.max_hp ?? "—")}</strong></div>
          <div class="detail-fact"><span>卡组 / 遗物</span><strong>${escapeHTML(
            run.deck_count,
          )} / ${escapeHTML(run.relic_count)}</strong></div>
          <div class="detail-fact"><span>完成时间</span><strong>${escapeHTML(
            formatFullTimestamp(run.ended_at),
          )}</strong></div>
          <div class="detail-fact"><span>审计问题</span><strong>${escapeHTML(
            auditCount ?? "未知",
          )}</strong></div>
          <div class="detail-fact"><span>待确认项</span><strong>${escapeHTML(
            unknownCount ?? "未知",
          )}</strong></div>
        </div>
      </article>
    </section>

    <section class="detail-section model-detail-section">
      <div class="detail-section-heading">
        <h3>模型调用</h3>
        <span>最终采纳仅统计改变最终语义决策的建议</span>
      </div>
      <div class="model-stat-grid">
        <div class="model-stat"><span>实际调用</span><strong>${escapeHTML(
          formatNumber(model.remote_consultations, "0"),
        )}</strong></div>
        <div class="model-stat"><span>有效建议</span><strong>${escapeHTML(
          formatNumber(model.remote_valid_recommendations, "0"),
        )}</strong></div>
        <div class="model-stat is-adopted"><span>最终采纳</span><strong>${escapeHTML(
          formatNumber(model.semantic_choice_changes, "0"),
        )}</strong></div>
        <div class="model-stat"><span>采纳率</span><strong>${escapeHTML(
          formatPercent(model.semantic_adoption_rate),
        )}</strong></div>
        <div class="model-stat"><span>与本地一致</span><strong>${escapeHTML(
          formatNumber(model.model_agreements, "0"),
        )}</strong></div>
        <div class="model-stat"><span>覆盖本地规则</span><strong>${escapeHTML(
          formatNumber(model.effective_overrides, "0"),
        )}</strong></div>
        <div class="model-stat"><span>回退</span><strong>${escapeHTML(
          formatNumber(model.fallbacks, "0"),
        )}</strong></div>
        <div class="model-stat"><span>本地缓存命中</span><strong>${escapeHTML(
          formatNumber(model.local_cache_hits, "0"),
        )}</strong></div>
      </div>
      <p class="model-footnote">远程延迟：平均 ${escapeHTML(
        formatNumber(Math.round(Number(model.average_latency_ms) || 0), "0"),
      )} ms，最高 ${escapeHTML(
        formatNumber(Math.round(Number(model.maximum_latency_ms) || 0), "0"),
      )} ms · 估算费用 ${escapeHTML(formatCost(model.estimated_cost_usd))}</p>
    </section>

    <section class="detail-section">
      <div class="detail-section-heading">
        <h3>卡组</h3>
        <span>${escapeHTML(run.deck_count)} 张</span>
      </div>
      <div class="resource-list">${resourceChips(build.deck, "card")}</div>
    </section>

    <section class="detail-section">
      <div class="detail-section-heading">
        <h3>遗物与药水</h3>
        <span>${escapeHTML(run.relic_count)} 件遗物</span>
      </div>
      <div class="resource-list">${resourceChips(build.relics, "relic")}</div>
      <div class="resource-list">${resourceChips(
        build.potions,
        "potion",
      )}</div>
    </section>

    ${replaySection}
    ${findingSection}

    <div class="technical-line">
      attempt ${escapeHTML(run.attempt_id)}<br />
      policy ${escapeHTML(run.policy_version)} · decision ${escapeHTML(
        payload.selection?.decision_hash || "—",
      )} · controller ${escapeHTML(payload.selection?.controller_hash || "—")}
    </div>`;
}

async function openRunDetail(attemptId) {
  if (!attemptId) return;
  elements.dialogKicker.textContent = "RUN DETAIL";
  elements.dialogTitle.textContent = "对局详情";
  elements.dialogContent.innerHTML = '<div class="detail-loading">正在读取这局的完成归档…</div>';
  if (!elements.dialog.open) elements.dialog.showModal();
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(attemptId)}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderDetail(await response.json());
  } catch (error) {
    elements.dialogTitle.textContent = "详情读取失败";
    elements.dialogContent.innerHTML =
      '<div class="detail-loading">这份完成归档暂时无法读取，请关闭后重试。</div>';
  }
}

document.querySelectorAll(".filter-button").forEach((button) => {
  button.addEventListener("click", () => {
    state.filter = button.dataset.character || "ALL";
    document.querySelectorAll(".filter-button").forEach((candidate) => {
      candidate.classList.toggle("is-active", candidate === button);
    });
    renderRuns();
  });
});

elements.refreshButton.addEventListener("click", () => loadRuns());
elements.dialogClose.addEventListener("click", () => elements.dialog.close());
elements.dialog.addEventListener("click", (event) => {
  if (event.target === elements.dialog) elements.dialog.close();
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") loadRuns({ quiet: true });
});

loadRuns();
window.setInterval(() => loadRuns({ quiet: true }), 30_000);
