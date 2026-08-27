const state = { jobs: [], filter: "", timer: null };
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));

const kindConfigs = {
  product: { label: "ASIN 或 Amazon 商品链接", placeholder: "每行一个，例如：\nB0XXXXXXXX\nhttps://www.amazon.com/dp/B0YYYYYYYY", help: "商品详情与变体维度会写入同一条幂等结果。", fields: [] },
  product_hw: { label: "ASIN 或 Amazon 商品链接", placeholder: "每行一个 ASIN 或 Amazon 商品链接", help: "保留旧独立工作通道语义，结果契约与商品详情一致。", fields: [] },
  product_time: { label: "ASIN 或 Amazon 商品链接", placeholder: "每行一个 ASIN 或 Amazon 商品链接", help: "每次任务是一条实时观测；建议使用 Realtime 执行策略。", fields: [] },
  search: { label: "搜索关键词", placeholder: "每行一个关键词，例如：\nwireless mouse\nportable monitor", help: "每个关键词和页码形成一个可恢复采集项。", fields: ["page", "frequent"] },
  search_hour: { label: "小时快照关键词", placeholder: "每行一个关键词", help: "系统保存整点观测时间和关键词页快照。", fields: ["page", "frequent"] },
  reviews: { label: "商品 ASIN", placeholder: "每行一个 ASIN，例如：\nB0XXXXXXXX", help: "采集商品详情响应中的评论区，并保留旧版原始行语义。", fields: [] },
  category_asin_list: { label: "类目 ID", placeholder: "每行一个类目 ID，例如：\n172282", help: "每个类目和页码形成一个持久化断点。", fields: ["page"] },
  rank_list: { label: "Amazon 榜单 URL", placeholder: "每行一个所选站点的 Amazon HTTPS 榜单 URL", help: "URL 必须属于所选 Amazon 站点，不接受任意外部地址。", fields: ["page", "category", "rankType"] },
  merchant: { label: "Seller ID", placeholder: "每行一个 Seller ID", help: "采集商家名称、企业信息与地址。", fields: [] },
  merchant_home: { label: "Seller ID", placeholder: "每行一个 Seller ID", help: "发现商家商品分页任务；可选指定同站点 Amazon URL。", fields: ["sourceTask", "objectUrl"] },
  merchant_products: { label: "Seller ID", placeholder: "每行一个 Seller ID", help: "采集指定商家分页商品列表。", fields: ["page", "sourceTask"] },
};

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const payload = await response.json();
  if (!response.ok || !payload.ok) throw new Error(payload.error?.message || `HTTP ${response.status}`);
  return payload;
}

function toast(message, error = false) {
  const el = $("#toast"); el.textContent = message; el.className = `toast show${error ? " error" : ""}`;
  setTimeout(() => el.className = "toast", 2600);
}

const statusLabel = status => ({pending:"待执行",running:"执行中",pause_requested:"暂停中",paused:"已暂停",cancel_requested:"取消中",cancelled:"已取消",succeeded:"已完成",partial:"部分完成",failed:"失败"}[status] || status);
const canPause = s => ["pending", "running"].includes(s);
const canResume = s => ["paused", "pause_requested"].includes(s);
const canCancel = s => !["cancelled", "succeeded", "partial", "failed"].includes(s);

async function loadCapabilities() {
  const data = await api("/api/v1/capabilities");
  const select = $("#marketplace");
  data.marketplaces.forEach(market => select.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(market.id)}">${escapeHtml(market.id)} · ${escapeHtml(market.name)}</option>`));
  const labels = {
    jsonl: ["JSONL 数据流", "便于数据分析和后续系统消费"],
    legacy_redis: ["旧 Redis 缓冲", "兼容现有数据消费链路"],
    legacy_mysql: ["旧 MySQL 表", "兼容现有业务表结构"],
  };
  const configured = data.result_storage?.configured_sinks || ["sqlite"];
  configured.filter(name => name !== "sqlite").forEach(name => {
    const [title, detail] = labels[name] || [name, "已配置的结果存储"];
    $("#resultSinkOptions").insertAdjacentHTML("beforeend", `<label class="sink-option"><input type="checkbox" value="${escapeHtml(name)}" /><span><b>${escapeHtml(title)}</b><small>${escapeHtml(detail)}</small></span></label>`);
  });
}

async function loadMetrics() {
  const { metrics } = await api("/api/v1/metrics");
  const total = Object.values(metrics.jobs_by_status).reduce((a, b) => a + b, 0);
  const active = (metrics.jobs_by_status.running || 0) + (metrics.jobs_by_status.pending || 0);
  $("#metricJobs").textContent = total; $("#metricResults").textContent = metrics.results;
  $("#metricCoverage").textContent = metrics.average_core_field_coverage == null ? "—" : `${Math.round(metrics.average_core_field_coverage * 100)}%`;
  $("#activeCount").textContent = active;
}

async function loadResourceHealth() {
  const health = await api("/api/v1/health");
  $("#metricCookies").textContent = health.resources.cookie.available;
  $("#healthText").textContent = health.resources.cookie.stale ? "系统就绪 · Cookie 待刷新" : "系统就绪 · 自动刷新";
}

async function loadJobs() {
  const query = state.filter ? `?status=${encodeURIComponent(state.filter)}` : "";
  const data = await api(`/api/v1/jobs${query}`); state.jobs = data.jobs; renderJobs();
}

function renderJobs() {
  const list = $("#jobList");
  if (!state.jobs.length) { list.innerHTML = '<div class="empty">当前筛选下没有任务</div>'; return; }
  list.innerHTML = state.jobs.map(job => `
    <article class="job" data-id="${escapeHtml(job.id)}">
      <div class="job-head"><strong>${escapeHtml(job.id)}</strong><span class="badge ${escapeHtml(job.status)}">${escapeHtml(statusLabel(job.status))}</span></div>
      <div class="job-meta"><span>${escapeHtml(job.kind)}</span><span>·</span><span>${escapeHtml(job.execution_mode.toUpperCase())}</span><span>·</span><span>${job.succeeded_items}/${job.total_items} 成功</span><span>·</span><span>CP ${job.checkpoint_seq}</span></div>
      <div class="job-sinks">存储 · ${escapeHtml((job.options?.result_sinks || ["sqlite"]).join(" + "))}</div>
      <div class="progress"><i style="width:${job.progress.percent}%"></i></div>
      <div class="job-actions">
        ${canPause(job.status) ? '<button data-action="pause">暂停</button>' : ''}
        ${canResume(job.status) ? '<button data-action="resume">恢复</button>' : ''}
        ${canCancel(job.status) ? '<button data-action="cancel">取消</button>' : ''}
        <button data-action="detail">证据与详情</button>
      </div>
    </article>`).join("");
}

async function control(jobId, action) {
  if (action === "cancel" && !window.confirm("确认取消该任务？已完成的结果会保留，尚未执行的项目将被标记为取消。")) return;
  await api(`/api/v1/jobs/${encodeURIComponent(jobId)}/${action}`, { method: "POST", body: "{}" });
  toast({pause:"已请求安全暂停",resume:"任务已恢复",cancel:"已请求取消"}[action]);
  await refresh();
}

function inputSummary(input, fallback) {
  return input.asin || input.keyword || input.category_id || input.seller_id || input.url || fallback;
}

function resultSummary(result) {
  const data = result.data || {};
  const item = data.item || data;
  const rows = Number(data.row_count ?? data.items?.length ?? data.child_tasks?.length ?? 0);
  const title = item.title || item.seller_name || item.asin || data.task?.keyword || result.input_key;
  const values = [];
  if (item.price || item.selling_price) values.push(item.price || item.selling_price);
  if (item.availability) values.push(item.availability);
  if (rows) values.push(`${rows} 行`);
  values.push(result.schema_version);
  const quality = item.quality?.core_field_coverage;
  if (quality != null) values.push(`Coverage ${Math.round(quality * 100)}%`);
  const hash = String(result.evidence?.sha256 || "").slice(0, 12);
  if (hash) values.push(`SHA ${hash}`);
  return { title, detail: values.join(" · ") };
}

function eventSummary(event) {
  const values = [event.event_type];
  if (event.payload?.code) values.push(event.payload.code);
  const hash = String(event.payload?.details?.evidence?.sha256 || "").slice(0, 12);
  if (hash) values.push(`SHA ${hash}`);
  return values.join(" · ");
}

const deliveryLabel = status => ({pending:"待分发",running:"分发中",delivered:"已送达",dead_letter:"需处理"}[status] || status);

function deliverySummary(delivery) {
  const receipt = delivery.receipt || {};
  const counts = ["result_rows", "dimension_rows", "child_task_rows", "product_task_rows"]
    .filter(key => Number(receipt[key] || 0) > 0)
    .map(key => `${key} ${receipt[key]}`);
  if (receipt.file) counts.push(receipt.file);
  if (receipt.deduplicated) counts.push("已去重");
  return counts.join(" · ") || `尝试 ${delivery.attempts}/${delivery.max_attempts}`;
}

async function showDetail(jobId) {
  const [{ job }, { results }, { events }, { deliveries }] = await Promise.all([
    api(`/api/v1/jobs/${encodeURIComponent(jobId)}`),
    api(`/api/v1/jobs/${encodeURIComponent(jobId)}/results`),
    api(`/api/v1/jobs/${encodeURIComponent(jobId)}/events?limit=30`),
    api(`/api/v1/jobs/${encodeURIComponent(jobId)}/deliveries?limit=30`),
  ]);
  $("#drawerTitle").textContent = job.id;
  $("#drawerBody").innerHTML = `
    <div class="detail-grid">
      <div><span>状态</span><strong>${escapeHtml(statusLabel(job.status))}</strong></div><div><span>进度</span><strong>${job.progress.percent}%</strong></div>
      <div><span>断点序号</span><strong>${job.checkpoint_seq} / ${job.total_items}</strong></div><div><span>任务 / 模式</span><strong>${escapeHtml(job.kind)} · ${escapeHtml(job.execution_mode)}</strong></div>
      <div><span>成功 / 失败</span><strong>${job.succeeded_items} / ${job.failed_items}</strong></div><div><span>创建时间</span><strong>${new Date(job.created_at).toLocaleString()}</strong></div>
    </div>
    <h3>采集项目</h3>${job.items.map(item => `<div class="item-row">#${item.seq} · ${escapeHtml(inputSummary(item.input, item.input_key))} · ${escapeHtml(statusLabel(item.status))} · TRY ${item.attempts}/${item.max_attempts}${item.last_error_code ? `<br><span style="color:#ff9f43">${escapeHtml(item.last_error_code)} · ${escapeHtml(item.last_error)}</span>` : ''}</div>`).join("")}
    <h3>结果证据</h3>${results.length ? results.map(result => { const summary = resultSummary(result); return `<div class="result-card"><strong>${escapeHtml(summary.title)}</strong><p>${escapeHtml(summary.detail)}</p></div>`; }).join("") : '<div class="empty" style="padding:30px">暂无结果</div>'}
    <h3>结果存储</h3><div class="result-card"><strong>sqlite · 已提交</strong><p>任务、断点与结果原本位于本地控制库</p></div>${deliveries.length ? deliveries.map(delivery => `<div class="result-card delivery ${escapeHtml(delivery.status)}"><strong>${escapeHtml(delivery.sink_name)} · ${escapeHtml(deliveryLabel(delivery.status))}</strong><p>${escapeHtml(deliverySummary(delivery))}${delivery.last_error ? ` · ${escapeHtml(delivery.last_error)}` : ""}</p></div>`).join("") : '<p class="input-help">本任务未选择其他结果去向。</p>'}
    <h3>状态事件</h3>${events.map(event => `<div class="event-row"><time>${new Date(event.created_at).toLocaleString()}</time>${escapeHtml(eventSummary(event))}${event.item_id ? ` · ${escapeHtml(event.item_id.slice(0, 16))}` : ''}</div>`).join("")}
  `;
  $("#drawer").classList.add("open"); $("#drawerBackdrop").classList.add("open");
}

async function refresh() { try { await Promise.all([loadJobs(), loadMetrics(), loadResourceHealth()]); } catch (error) { toast(error.message, true); } }
function closeDrawer() { $("#drawer").classList.remove("open"); $("#drawerBackdrop").classList.remove("open"); }

function applyKindConfig() {
  const kind = $("#kind").value;
  const config = kindConfigs[kind];
  $("#inputLabel").textContent = config.label;
  $("#inputs").placeholder = config.placeholder;
  $("#inputHelp").textContent = config.help;
  document.querySelectorAll(".task-field").forEach(field => {
    field.classList.toggle("visible", config.fields.includes(field.dataset.fields));
  });
  $("#categoryId").required = config.fields.includes("category");
  if (kind === "product_time") {
    $("#mode").value = "realtime";
    $("#formNote").textContent = "每次提交都会创建一条新的实时观测；API 调用方可用幂等键安全重试。凭证和代理只能由部署环境注入。";
  } else {
    $("#formNote").textContent = "相同参数会自动返回同一任务，不会重复创建；凭证和代理只能由部署环境注入。";
  }
}

function buildInputs() {
  const kind = $("#kind").value;
  const lines = $("#inputs").value.split(/\n/).map(value => value.trim()).filter(Boolean);
  const market = $("#marketplace").value;
  const postal = $("#postalCode").value.trim();
  if (!lines.length) throw new Error("请至少填写一个采集输入");
  if (!["product", "product_hw", "product_time"].includes(kind) && !market) throw new Error("该任务类型必须选择站点");
  if (["product", "product_hw", "product_time"].includes(kind)) return lines;
  const page = Number($("#page").value);
  return lines.map(value => {
    const common = { market_id: market };
    if (postal) common.post_code = postal;
    if (["search", "search_hour"].includes(kind)) return { ...common, keyword: value, turn_page: page, frequent: Number($("#frequent").value) };
    if (kind === "reviews") return { ...common, asin: value };
    if (kind === "category_asin_list") return { ...common, category_id: value, page };
    if (kind === "rank_list") return { ...common, url: value, url_type: $("#rankType").value, category_id: $("#categoryId").value.trim(), page };
    const merchant = { ...common, seller_id: value };
    const sourceTaskId = $("#sourceTaskId").value.trim();
    if (sourceTaskId) merchant.source_task_id = sourceTaskId;
    if (kind === "merchant_home") {
      const objectUrl = $("#objectUrl").value.trim();
      if (objectUrl) merchant.object_url = objectUrl;
    }
    if (kind === "merchant_products") merchant.page = page;
    return merchant;
  });
}

function selectedResultSinks() {
  return [...document.querySelectorAll("#resultSinkOptions input:checked")].map(input => input.value);
}

function retryOptions() {
  const value = $("#maxAttempts").value.trim();
  return value ? { max_attempts: Number(value) } : {};
}

$("#createForm").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.currentTarget.querySelector("button[type=submit]"); button.disabled = true;
  try {
    const data = await api("/api/v1/jobs", { method: "POST", body: JSON.stringify({ kind: $("#kind").value, inputs: buildInputs(), marketplace_id: $("#marketplace").value || null, postal_code: $("#postalCode").value || null, execution_mode: $("#mode").value, ...retryOptions(), options: { result_sinks: selectedResultSinks() } }) });
    toast(data.created ? "任务已创建并持久化" : "相同任务已存在，已返回原任务"); $("#inputs").value = ""; await refresh(); showDetail(data.job.id);
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
});

$("#jobList").addEventListener("click", event => {
  const job = event.target.closest(".job"); if (!job) return; const action = event.target.dataset.action || "detail";
  action === "detail" ? showDetail(job.dataset.id).catch(e => toast(e.message, true)) : control(job.dataset.id, action).catch(e => toast(e.message, true));
});
document.querySelectorAll(".filter").forEach(button => button.addEventListener("click", () => { document.querySelectorAll(".filter").forEach(b => b.classList.remove("active")); button.classList.add("active"); state.filter = button.dataset.status; refresh(); }));
$("#kind").addEventListener("change", applyKindConfig); $("#refreshButton").addEventListener("click", refresh); $("#drawerClose").addEventListener("click", closeDrawer); $("#drawerBackdrop").addEventListener("click", closeDrawer);

(async function boot() {
  try { applyKindConfig(); await loadCapabilities(); await refresh(); state.timer = setInterval(refresh, 5000); }
  catch (error) { $("#healthText").textContent = "内核不可用"; toast(error.message, true); }
})();
