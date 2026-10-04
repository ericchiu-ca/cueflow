const $ = (id) => document.getElementById(id);
let csrfToken = "";
const state = {
  video: null,
  translateSourceFile: null,
  translateSourceText: null,
  translateSourceName: null,
  reviewSourceFile: null,
  reviewSourceText: null,
  reviewSourceName: null,
  reviewChineseFile: null,
  reviewChineseText: null,
  reviewChineseName: null,
  reviewId: null,
  reviewTracks: { source: [], chinese: [] },
  reviewTrack: "source",
  reviewIndices: { source: 0, chinese: 0 },
  reviewHistory: [],
  asrQualityIssues: {},
  sourceFile: null,
  sourceText: null,
  sourceName: null,
  zhFile: null,
  zhText: null,
  zhName: null,
  reviewPairMatched: null,
  reviewPairing: null,
  assId: null,
  assFile: null,
  burnVideo: null,
};
function bindDrop(zoneId, inputId, onFile) {
  const zone = $(zoneId),
    input = $(inputId);
  zone.addEventListener("click", () => input.click());
  input.addEventListener(
    "change",
    () => input.files[0] && onFile(input.files[0]),
  );
  ["dragenter", "dragover"].forEach((n) =>
    zone.addEventListener(n, (e) => {
      e.preventDefault();
      zone.classList.add("drag");
    }),
  );
  ["dragleave", "drop"].forEach((n) =>
    zone.addEventListener(n, (e) => {
      e.preventDefault();
      zone.classList.remove("drag");
    }),
  );
  zone.addEventListener(
    "drop",
    (e) => e.dataTransfer.files[0] && onFile(e.dataTransfer.files[0]),
  );
}
const size = (b) =>
  b < 1048576
    ? `${(b / 1024).toFixed(0)} KB`
    : `${(b / 1048576).toFixed(1)} MB`;
function setStatus(
  prefix,
  visible,
  percent,
  message,
  detail = "",
  failed = false,
) {
  $(`${prefix}Status`).classList.toggle("visible", visible);
  $(`${prefix}Bar`).style.width = `${Math.max(0, Math.min(100, percent))}%`;
  $(`${prefix}Percent`).textContent = `${percent}%`;
  $(`${prefix}Message`).textContent = message;
  $(`${prefix}Message`).classList.toggle("error", failed);
  $(`${prefix}Detail`).textContent = detail;
  $(`${prefix}Detail`).classList.toggle("error", failed);
}
function updateButtons() {
  const sourceReady = !!(state.sourceText || state.sourceFile),
    zhReady = !!(state.zhText || state.zhFile);
  $("transcribeButton").disabled = !state.video;
  $("translateButton").disabled = !(
    state.translateSourceText || state.translateSourceFile
  );
  $("reviewLoadButton").disabled = !(
    state.reviewSourceText || state.reviewSourceFile
  );
  $("assButton").disabled =
    !sourceReady || !zhReady || state.reviewPairMatched === false;
  $("burnButton").disabled =
    !state.burnVideo || !(state.assId || state.assFile);
  $("assReadiness").textContent =
    state.reviewPairMatched === false
      ? "审校后的两轨数量或时间未对齐，请返回审校台继续调整。"
      : "两轨有效段落数必须一致，时间轴继承原文轨。";
}
async function json(url, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") {
    const headers = new Headers(options.headers || {});
    headers.set("X-CueFlow-CSRF", csrfToken);
    options = { ...options, headers };
  }
  const response = await fetch(url, options);
  let payload;
  try {
    payload = await response.json();
  } catch (_) {
    payload = { error: `HTTP ${response.status}` };
  }
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}
function upload(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader(
      "Content-Type",
      file.type || "application/octet-stream",
    );
    xhr.setRequestHeader("X-CueFlow-CSRF", csrfToken);
    xhr.upload.onprogress = (e) =>
      e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100));
    xhr.onload = () => {
      let p;
      try {
        p = JSON.parse(xhr.responseText);
      } catch (_) {
        p = { error: `HTTP ${xhr.status}` };
      }
      xhr.status >= 200 && xhr.status < 300
        ? resolve(p)
        : reject(new Error(p.error || `HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("无法连接本地 CueFlow 服务"));
    xhr.send(file);
  });
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function poll(kind, id, prefix) {
  let networkFailures = 0;
  while (true) {
    let job;
    try {
      job = await json(`/api/${kind}/${id}`);
      networkFailures = 0;
    } catch (error) {
      if (!(error instanceof TypeError) || ++networkFailures > 10) throw error;
      setStatus(
        prefix,
        true,
        0,
        "与本地服务的连接中断，正在重试…",
        `第 ${networkFailures} 次重试`,
      );
      await sleep(1500);
      continue;
    }
    setStatus(
      prefix,
      true,
      job.percent || 0,
      job.message || "处理中",
      job.error || "",
      job.status === "failed",
    );
    if (job.status === "complete") return job;
    if (["failed", "cancelled", "interrupted"].includes(job.status))
      throw new Error(
        job.error || job.message || "任务已停止，可从任务记录恢复",
      );
    await sleep(900);
  }
}
async function environment() {
  try {
    const e = await json("/api/environment");
    csrfToken = e.csrf_token || "";
    const entries = [
      ["MLX Turbo", e.model.ready],
      ["MLX large-v3", e.large_model && e.large_model.ready],
      [
        `WhisperX${e.whisperx.version ? " " + e.whisperx.version : ""}`,
        e.whisperx.ready,
      ],
      ["FFmpeg + libass", e.ffmpeg.ready],
      ["内置字体", e.fonts.ready],
      ["Codex Pro", e.codex.ready],
    ];
    $("environment").innerHTML = entries
      .map(
        ([l, r]) =>
          `<span class="env-pill"><i class="dot ${r ? "ready" : ""}"></i>${l}${r ? "" : " 未就绪"}</span>`,
      )
      .join("");
    await Promise.all([loadSettings(), refreshJobs()]);
  } catch (error) {
    $("environment").textContent = `环境检查失败：${error.message}`;
  }
}

const resourceLabels = {
  model_path: "MLX Turbo 模型目录",
  large_model_path: "MLX large-v3 模型目录",
  helper_path: "MLX helper 可执行文件",
  whisperx_python: "WhisperX Python 可执行文件",
  ffmpeg_path: "FFmpeg 可执行文件",
};

async function loadSettings() {
  const config = await json("/api/config");
  $("settingsMessage").textContent = config.error || "";
  const fields = $("settingsFields");
  fields.replaceChildren();
  for (const [field, title] of Object.entries(resourceLabels)) {
    const label = document.createElement("label");
    label.textContent = title;
    const input = document.createElement("input");
    input.id = `setting-${field}`;
    input.name = field;
    input.value = config.resources[field] || "";
    input.placeholder = config.effective[field].path || "留空自动发现";
    input.autocomplete = "off";
    input.setAttribute("list", `choices-${field}`);
    const choices = document.createElement("datalist");
    choices.id = `choices-${field}`;
    for (const path of config.effective[field].candidates) {
      const option = document.createElement("option");
      option.value = path;
      choices.append(option);
    }
    const status = document.createElement("small");
    const sources = {
      command: "启动参数",
      environment: "环境变量",
      saved: "已保存",
      detected: "自动发现",
      fallback: "自动回退",
    };
    const effective = config.effective[field];
    status.textContent = `${effective.ready ? "路径可用" : "未就绪"} · ${sources[effective.source]} · ${effective.path || "未指定"}`;
    label.append(input, choices, status);
    fields.append(label);
  }
}

$("settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("settingsSave");
  button.disabled = true;
  try {
    const resources = Object.fromEntries(
      Object.keys(resourceLabels).map((field) => [
        field,
        $(`setting-${field}`).value,
      ]),
    );
    await json("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resources }),
    });
    await environment();
    $("settingsMessage").textContent = "配置已保存";
  } catch (error) {
    $("settingsMessage").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

let refreshingJobs = false;
async function refreshJobs() {
  if (refreshingJobs) return;
  refreshingJobs = true;
  try {
    const result = await json("/api/jobs");
    const list = $("jobsList");
    list.replaceChildren();
    const names = {
      uploading: "接收文件",
      resuming: "准备恢复",
      queued: "排队",
      running: "处理中",
      cancelling: "正在取消",
      cancelled: "已取消",
      interrupted: "已中断",
      failed: "失败",
      complete: "完成",
    };
    for (const job of result.jobs.slice(0, 30)) {
      const row = document.createElement("div");
      row.className = "job-row";
      row.dataset.jobId = job.id;
      const title = document.createElement("span");
      title.textContent = `${job.source_name || job.kind} · ${names[job.status] || job.status} · ${job.percent || 0}%`;
      row.append(title);
      const message = document.createElement("small");
      message.textContent = job.error || job.message || "";
      row.append(message);
      const active = ["resuming", "queued", "running", "cancelling"].includes(
        job.status,
      );
      const resumable = ["failed", "interrupted", "cancelled"].includes(
        job.status,
      );
      if (active || resumable) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = active ? "取消任务" : "恢复任务";
        button.disabled = job.status === "cancelling";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            await json(
              `/api/jobs/${encodeURIComponent(job.id)}/${active ? "cancel" : "resume"}`,
              { method: "POST" },
            );
            await refreshJobs();
          } catch (error) {
            $("jobsMessage").textContent = error.message;
            button.disabled = false;
          }
        });
        row.append(button);
      }
      if (
        job.status === "complete" &&
        /^\/api\//.test(job.download_url || "")
      ) {
        const download = document.createElement("a");
        download.href = job.download_url;
        download.textContent = "下载结果";
        download.download = job.download_name || "";
        row.append(download);
      }
      list.append(row);
    }
    if (!result.jobs.length) list.textContent = "暂无任务";
    $("jobsMessage").textContent = result.warnings.join("；");
  } catch (error) {
    $("jobsMessage").textContent = `任务记录暂时无法读取：${error.message}`;
  } finally {
    refreshingJobs = false;
  }
}
setInterval(refreshJobs, 3000);

$("cleanupButton").addEventListener("click", async () => {
  const unsaved = hasUnsavedReview()
    ? "\n\n注意：审校台还有未保存的修改，清理并刷新后会丢失。"
    : "";
  if (
    !confirm(
      "将永久删除 CueFlow 在 projects/local 中的全部上传文件、项目文件和生成结果。浏览器已经下载的文件不受影响。继续清理？" +
        unsaved,
    )
  )
    return;
  const button = $("cleanupButton");
  button.disabled = true;
  $("cleanupMessage").textContent = "正在清理本地任务文件…";
  try {
    const result = await json("/api/cleanup", { method: "POST" });
    $("cleanupMessage").textContent =
      `已删除 ${result.removed_files} 个文件，释放 ${size(result.freed_bytes)}`;
    alert(
      `清理完成：删除 ${result.removed_files} 个文件，释放 ${size(result.freed_bytes)}。`,
    );
    skipUnloadGuard = true;
    window.location.reload();
  } catch (error) {
    $("cleanupMessage").textContent = `清理失败：${error.message}`;
    button.disabled = false;
  }
});

bindDrop("videoDrop", "videoInput", (file) => {
  state.video = file;
  $("videoLabel").textContent = file.name;
  $("videoMeta").textContent = size(file.size);
  updateButtons();
});
bindDrop("translateSourceDrop", "translateSourceInput", async (file) => {
  state.translateSourceFile = file;
  state.translateSourceText = await file.text();
  state.translateSourceName = file.name;
  $("translateSourceLabel").textContent = file.name;
  $("translateSourceMeta").textContent =
    `${size(file.size)} · 视频和音频不上传`;
  updateButtons();
});
bindDrop("reviewSourceDrop", "reviewSourceInput", async (file) => {
  state.reviewSourceFile = file;
  state.reviewSourceText = await file.text();
  state.reviewSourceName = file.name;
  $("reviewSourceLabel").textContent = file.name;
  $("reviewSourceMeta").textContent = `${size(file.size)} · 等待载入`;
  updateButtons();
});
bindDrop("reviewChineseDrop", "reviewChineseInput", async (file) => {
  state.reviewChineseFile = file;
  state.reviewChineseText = await file.text();
  state.reviewChineseName = file.name;
  $("reviewChineseLabel").textContent = file.name;
  $("reviewChineseMeta").textContent = `${size(file.size)} · 将作为中文轨`;
  updateButtons();
});
bindDrop("sourceDrop", "sourceInput", async (file) => {
  state.sourceFile = file;
  state.sourceText = await file.text();
  state.sourceName = file.name;
  state.reviewPairMatched = null;
  $("sourceLabel").textContent = file.name;
  $("sourceMeta").textContent = `${size(file.size)} · 用户选择`;
  updateButtons();
});
bindDrop("zhDrop", "zhInput", async (file) => {
  state.zhFile = file;
  state.zhText = await file.text();
  state.zhName = file.name;
  state.reviewPairMatched = null;
  $("zhLabel").textContent = file.name;
  $("zhMeta").textContent = `${size(file.size)} · 用户选择`;
  updateButtons();
});
bindDrop("burnVideoDrop", "burnVideoInput", (file) => {
  state.burnVideo = file;
  $("burnVideoLabel").textContent = file.name;
  $("burnVideoMeta").textContent = size(file.size);
  updateButtons();
});
bindDrop("burnAssDrop", "burnAssInput", (file) => {
  state.assFile = file;
  state.assId = null;
  $("burnAssLabel").textContent = file.name;
  $("burnAssMeta").textContent = `${size(file.size)} · 用户选择`;
  updateButtons();
});

$("transcribeButton").addEventListener("click", async () => {
  $("transcribeButton").disabled = true;
  $("transcribeResult").classList.remove("visible");
  try {
    const q = new URLSearchParams({
      filename: state.video.name,
      language: $("language").value,
      alignment: $("alignment").value,
    });
    setStatus("transcribe", true, 0, "正在接收视频", state.video.name);
    const queued = await upload(`/api/jobs?${q}`, state.video, (p) =>
      setStatus(
        "transcribe",
        true,
        Math.min(12, Math.round(p * 0.12)),
        "正在接收视频",
        `${p}%`,
      ),
    );
    const job = await poll("jobs", queued.id, "transcribe"),
      quality = Array.isArray(job.quality_issues) ? job.quality_issues : [];
    $("srtDownload").href = job.download_url;
    $("srtDownload").download = job.download_name;
    $("transcribeResultText").innerHTML = quality.length
      ? `<strong>SRT 已生成，${quality.length} 条需审校</strong><br>${(job.alignment || "").startsWith("whisperx") ? "WhisperX 词级对齐" : "MLX 原生时间轴"} · 正在送入审校台`
      : `<strong>SRT 已生成</strong><br>${(job.alignment || "").startsWith("whisperx") ? "WhisperX 词级对齐" : "MLX 原生时间轴"} · 已接入翻译与审校台`;
    $("transcribeResult").classList.add("visible");
    const text = await fetch(job.download_url).then((r) => r.text());
    state.asrQualityIssues = quality.reduce((result, issue) => {
      if (issue.id) {
        (result[issue.id] ??= []).push({
          severity: issue.severity || "WARN",
          code: issue.code || "ASR_QUALITY",
          message: issue.message || "ASR 片段需要人工审校",
        });
      }
      return result;
    }, {});
    state.translateSourceText = text;
    state.translateSourceName = job.download_name;
    state.translateSourceFile = null;
    state.reviewSourceText = text;
    state.reviewSourceName = job.download_name;
    state.reviewSourceFile = null;
    state.sourceText = text;
    state.sourceName = job.download_name;
    $("translateSourceLabel").textContent = job.download_name;
    $("translateSourceMeta").textContent = "来自第 01 步 · 可直接翻译";
    $("reviewSourceLabel").textContent = job.download_name;
    $("reviewSourceMeta").textContent = quality.length
      ? `来自第 01 步 · ${quality.length} 条 ASR 异常待审`
      : "来自第 01 步 · 等待审校";
    $("sourceLabel").textContent = job.download_name;
    $("sourceMeta").textContent = "来自第 01 步";
    if (quality.length)
      setTimeout(() => {
        updateButtons();
        $("reviewLoadButton").click();
        location.hash = "review";
      }, 0);
  } catch (error) {
    setStatus("transcribe", true, 0, "生成失败", error.message, true);
  } finally {
    updateButtons();
  }
});

async function finishTranslation(job, source, sourceName) {
  const zh = await fetch(job.download_url).then((response) => {
    if (!response.ok) throw new Error("无法读取生成的中文字幕");
    return response.text();
  });
  state.reviewSourceText = source;
  state.reviewSourceName = sourceName;
  state.reviewSourceFile = null;
  state.sourceText = source;
  state.sourceName = sourceName;
  state.sourceFile = null;
  state.reviewChineseText = zh;
  state.reviewChineseName = "zh.srt";
  state.reviewChineseFile = null;
  state.zhText = zh;
  state.zhName = "zh.srt";
  state.zhFile = null;
  state.reviewPairMatched = null;
  $("reviewSourceLabel").textContent = sourceName;
  $("reviewSourceMeta").textContent = "来自第 02 步 · 等待审校";
  $("sourceLabel").textContent = sourceName;
  $("sourceMeta").textContent = "来自第 02 步";
  $("reviewChineseLabel").textContent = "zh.srt";
  $("reviewChineseMeta").textContent = "Codex 翻译 · 可继续审校";
  $("zhLabel").textContent = "zh.srt";
  $("zhMeta").textContent = "来自第 02 步";
  $("zhTranslationDownload").href = job.download_url;
  $("zhTranslationDownload").download = job.download_name;
  $("idTranslationDownload").href = job.translation_text_url;
  $("idTranslationDownload").download = job.translation_text_name;
  $("translateResultText").innerHTML =
    `<strong>中文字幕已生成</strong><br>${job.segment_count} 条 · ${job.model}${job.warnings.length ? ` · ${job.warnings.length} 条 QC 提醒` : ""}`;
  $("translateResult").classList.add("visible");
}
async function runTranslationJob(start, source, sourceName) {
  $("translateRetryButton").style.display = "none";
  let jobId = null;
  try {
    const queued = await start();
    jobId = queued.id;
    const job = await poll("translate", queued.id, "translate");
    state.translateFailed = null;
    await finishTranslation(job, source, sourceName);
  } catch (error) {
    setStatus("translate", true, 0, "翻译失败", error.message, true);
    if (jobId) {
      state.translateFailed = { id: jobId, source, sourceName };
      $("translateRetryButton").style.display = "inline-block";
    }
  } finally {
    updateButtons();
  }
}
$("translateRetryButton").addEventListener("click", () => {
  const failed = state.translateFailed;
  if (!failed) return;
  setStatus("translate", true, 2, "正在重试翻译", "已完成的批次会直接复用");
  runTranslationJob(
    () => json(`/api/translate/${failed.id}/retry`, { method: "POST" }),
    failed.source,
    failed.sourceName,
  );
});
$("translateButton").addEventListener("click", async () => {
  $("translateButton").disabled = true;
  $("translateResult").classList.remove("visible");
  let source, sourceName;
  try {
    source =
      state.translateSourceText || (await state.translateSourceFile.text());
    sourceName = state.translateSourceName || state.translateSourceFile.name;
  } catch (error) {
    setStatus("translate", true, 0, "无法读取源字幕", error.message, true);
    updateButtons();
    return;
  }
  setStatus("translate", true, 2, "正在准备翻译", "校验稳定 ID");
  return runTranslationJob(
    () =>
      json("/api/translate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_filename: sourceName,
          source_srt: source,
          source_language: $("translateLanguage").value,
          model: $("translationModel").value,
          provider: "codex",
        }),
      }),
    source,
    sourceName,
  );
});

const tracks = () => state.reviewTracks;
const currentSegments = () => tracks()[state.reviewTrack] || [];
const currentIndex = () => state.reviewIndices[state.reviewTrack] || 0;
const otherTrack = () =>
  state.reviewTrack === "source" ? "chinese" : "source";
function mergeIssues() {
  Object.entries(tracks()).forEach(([track, values]) =>
    values.forEach((s) => {
      if (!Array.isArray(s.ruleIssues))
        s.ruleIssues = Array.isArray(s.issues)
          ? s.issues.filter((issue) => !issue.asr)
          : [];
      const asr =
        track === "source" && !s.reviewed
          ? (state.asrQualityIssues[s.id] || []).map((issue) => ({
              ...issue,
              asr: true,
            }))
          : [];
      s.issues = [...asr, ...s.ruleIssues];
    }),
  );
}
let auditTimer = null,
  auditSeq = 0;
async function runAudit() {
  const seq = ++auditSeq,
    payload = {};
  Object.entries(tracks()).forEach(([track, values]) => {
    if (values.length)
      payload[track] = values.map((s) => ({
        id: s.id,
        start: s.start,
        end: s.end,
        text: s.text,
      }));
  });
  if (!Object.keys(payload).length) return;
  try {
    const result = await json("/api/review/audit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tracks: payload }),
    });
    if (seq !== auditSeq) return;
    state.reviewPairing = result.pairing || null;
    updatePairStatus();
    Object.entries(result.tracks || {}).forEach(([track, lists]) =>
      (tracks()[track] || []).forEach((s, i) => {
        s.ruleIssues = lists[i] || [];
      }),
    );
    mergeIssues();
    renderList();
    renderIssueViews();
  } catch (_) {}
}
function refreshIssues() {
  mergeIssues();
  clearTimeout(auditTimer);
  auditTimer = setTimeout(runAudit, 120);
}
function commitCurrent(mark = false) {
  const values = currentSegments(),
    i = currentIndex(),
    segment = values[i];
  if (!segment) return;
  const start = Number($("reviewStart").value),
    end = Number($("reviewEnd").value);
  if (Number.isFinite(start)) segment.start = start;
  if (Number.isFinite(end)) segment.end = end;
  segment.text = $("reviewText").value;
  if (mark) segment.reviewed = true;
  refreshIssues();
}
function updatePairStatus() {
  const source = tracks().source.length,
    chinese = tracks().chinese.length,
    hasChinese = chinese > 0,
    p = state.reviewPairing,
    fresh = p && p.source_count === source && p.chinese_count === chinese,
    drift = hasChinese && source === chinese && fresh && !p.matched,
    matched = !hasChinese || (source === chinese && !drift);
  $("sourceTrackCount").textContent = source;
  $("chineseTrackCount").textContent = chinese;
  $("chineseTrackTab").disabled = !hasChinese;
  $("reviewDeleteBothButton").disabled = !hasChinese;
  $("pairStatus").classList.toggle("mismatch", hasChinese && !matched);
  $("pairStatus").textContent = !hasChinese
    ? `仅原文轨 · ${source} 条`
    : matched
      ? `双轨已对齐 · 各 ${source} 条`
      : drift
        ? `时间错位 · 从 ${p.first_divergence ? p.first_divergence.id : (p.drifted_ids || [])[0]} 开始`
        : `数量不一致 · 原文 ${source} / 中文 ${chinese}`;
  return matched;
}
function renderList() {
  const box = $("reviewList"),
    filter = $("reviewFilter").value,
    query = $("reviewSearch").value.trim().toLocaleLowerCase(),
    values = currentSegments();
  box.replaceChildren();
  values.forEach((segment, index) => {
    if (filter === "issues" && !segment.issues.length) return;
    if (filter === "pending" && segment.reviewed) return;
    if (
      query &&
      !segment.id.includes(query) &&
      !segment.text.toLocaleLowerCase().includes(query)
    )
      return;
    const b = document.createElement("button");
    b.type = "button";
    b.className = `review-item${index === currentIndex() ? " active" : ""}`;
    const id = document.createElement("span");
    id.className = "review-id";
    id.textContent = segment.id;
    const text = document.createElement("span");
    text.className = "review-snippet";
    text.textContent = segment.text || "（空字幕）";
    const count = document.createElement("span");
    count.className = `review-count${segment.issues.length ? " issue" : segment.reviewed ? " checked" : ""}`;
    count.textContent = segment.issues.length
      ? segment.issues.length
      : segment.reviewed
        ? "✓"
        : "·";
    b.append(id, text, count);
    b.addEventListener("click", () => openSegment(index));
    box.append(b);
  });
}
function renderEditor() {
  const values = currentSegments(),
    i = Math.min(currentIndex(), Math.max(0, values.length - 1));
  state.reviewIndices[state.reviewTrack] = i;
  const segment = values[i];
  if (!segment) return;
  $("reviewCurrentId").textContent =
    `[${segment.id}] · ${state.reviewTrack === "source" ? "原文轨" : "中文轨"}${segment.reviewed ? " · 已审" : ""}`;
  $("reviewStart").value = segment.start;
  $("reviewEnd").value = segment.end;
  $("reviewText").value = segment.text;
  const other = tracks()[otherTrack()] || [],
    counterpart = other[i];
  $("counterpartLabel").textContent =
    `${otherTrack() === "source" ? "原文" : "中文"}轨同序对照${counterpart ? " · [" + counterpart.id + "]" : ""}`;
  $("counterpartText").textContent = counterpart
    ? counterpart.text
    : "当前序号没有对应字幕";
  $("reviewPrevButton").disabled = i <= 0;
  $("reviewNextButton").disabled = i >= values.length - 1;
  $("reviewMergeButton").disabled = i >= values.length - 1;
  $("reviewDeleteButton").disabled = values.length <= 1;
  renderIssueViews();
}
function renderIssueViews() {
  const values = currentSegments(),
    segment = values[Math.min(currentIndex(), Math.max(0, values.length - 1))];
  const issueBox = $("reviewIssues");
  issueBox.replaceChildren();
  if (segment) {
    if (!segment.issues.length) {
      const chip = document.createElement("span");
      chip.className = "issue-chip clear";
      chip.textContent = "未发现问题";
      issueBox.append(chip);
    }
    segment.issues.forEach((issue) => {
      const chip = document.createElement("span");
      chip.className = "issue-chip";
      chip.textContent = issue.message;
      issueBox.append(chip);
    });
  }
  const reviewed = values.filter((v) => v.reviewed).length,
    problem = values.filter((v) => v.issues.length).length;
  $("reviewStats").textContent =
    `${reviewed} / ${values.length} 已审 · ${problem} 条待注意`;
}
function render() {
  refreshIssues();
  document
    .querySelectorAll(".track-tab")
    .forEach((b) =>
      b.classList.toggle("active", b.dataset.track === state.reviewTrack),
    );
  updatePairStatus();
  renderList();
  renderEditor();
  $("reviewUndoButton").disabled = !state.reviewHistory.length;
}
function openSegment(index) {
  commitCurrent();
  state.reviewIndices[state.reviewTrack] = index;
  render();
}
function pushHistory() {
  commitCurrent();
  state.reviewHistory.push(
    JSON.stringify({
      tracks: state.reviewTracks,
      indices: state.reviewIndices,
      track: state.reviewTrack,
    }),
  );
  if (state.reviewHistory.length > 30) state.reviewHistory.shift();
}
function nextId(track) {
  const max = Math.max(
    0,
    ...tracks()[track].map((s) => Number.parseInt(s.id, 10) || 0),
  );
  return String(max + 1).padStart(4, "0");
}
function deleteCurrent(sync) {
  const values = currentSegments(),
    i = currentIndex();
  if (values.length <= 1) return;
  pushHistory();
  values.splice(i, 1);
  state.reviewIndices[state.reviewTrack] = Math.min(i, values.length - 1);
  if (sync && tracks()[otherTrack()].length > i) {
    tracks()[otherTrack()].splice(i, 1);
    state.reviewIndices[otherTrack()] = Math.min(
      state.reviewIndices[otherTrack()] || 0,
      Math.max(0, tracks()[otherTrack()].length - 1),
    );
  }
  render();
}
function addAfter() {
  pushHistory();
  const values = currentSegments(),
    i = currentIndex(),
    current = values[i],
    start = current.end,
    next = values[i + 1],
    end = next && next.start > start ? next.start : start + 2;
  values.splice(i + 1, 0, {
    id: nextId(state.reviewTrack),
    start,
    end,
    text: "",
    reviewed: false,
    issues: [],
  });
  state.reviewIndices[state.reviewTrack] = i + 1;
  render();
  $("reviewText").focus();
}
function splitCurrent() {
  const values = currentSegments(),
    i = currentIndex(),
    segment = values[i],
    editor = $("reviewText");
  commitCurrent();
  const text = segment.text,
    rawCaret = editor.selectionStart;
  if (text.trim().length < 2) {
    alert("当前字幕太短，无法拆分。");
    return;
  }
  let caret = rawCaret;
  if (caret <= 0 || caret >= text.length) {
    caret = Math.floor(text.length / 2);
    const right = text.indexOf(" ", caret),
      left = text.lastIndexOf(" ", caret);
    caret =
      right >= 0 && right - caret < caret - left
        ? right
        : left > 0
          ? left
          : caret;
  }
  const first = text.slice(0, caret).trim(),
    second = text.slice(caret).trim();
  if (!first || !second) {
    alert("请把光标放在希望拆分的位置。");
    return;
  }
  pushHistory();
  const originalEnd = segment.end,
    ratio = Math.min(0.8, Math.max(0.2, caret / text.length)),
    split = segment.start + (originalEnd - segment.start) * ratio;
  segment.end = Number(split.toFixed(3));
  segment.text = first;
  segment.reviewed = false;
  values.splice(i + 1, 0, {
    id: nextId(state.reviewTrack),
    start: segment.end,
    end: originalEnd,
    text: second,
    reviewed: false,
    issues: [],
  });
  state.reviewIndices[state.reviewTrack] = i + 1;
  render();
}
function mergeNext() {
  const values = currentSegments(),
    i = currentIndex();
  if (i >= values.length - 1) return;
  pushHistory();
  const current = values[i],
    next = values[i + 1];
  current.end = next.end;
  current.text =
    `${current.text}${state.reviewTrack === "chinese" ? "" : " "}${next.text}`.trim();
  current.reviewed = false;
  values.splice(i + 1, 1);
  render();
}
function undoStructure() {
  const raw = state.reviewHistory.pop();
  if (!raw) return;
  const snapshot = JSON.parse(raw);
  state.reviewTracks = snapshot.tracks;
  state.reviewIndices = snapshot.indices;
  state.reviewTrack = snapshot.track;
  render();
}

const reviewSnapshot = () =>
  JSON.stringify(
    Object.fromEntries(
      Object.entries(tracks()).map(([t, v]) => [
        t,
        v.map((s) => [s.id, s.start, s.end, s.text, !!s.reviewed]),
      ]),
    ),
  );
let skipUnloadGuard = false;
function markReviewSaved() {
  state.reviewSavedSnapshot = reviewSnapshot();
}
function hasUnsavedReview() {
  if (!state.reviewId || state.reviewSavedSnapshot == null) return false;
  commitCurrent();
  return reviewSnapshot() !== state.reviewSavedSnapshot;
}
window.addEventListener("beforeunload", (event) => {
  if (!skipUnloadGuard && hasUnsavedReview()) {
    event.preventDefault();
    event.returnValue = "";
  }
});
const pairingText = (p, ok, advice) => {
  if (p.matched) return ok;
  const at = p.first_divergence
    ? `，从原文第 ${p.first_divergence.id} 条开始错开`
    : "";
  if (p.source_count !== p.chinese_count)
    return `两轨数量不同（原文 ${p.source_count} / 中文 ${p.chinese_count}）${at}，${advice}`;
  return `两轨时间错位：${(p.drifted_ids || []).slice(0, 5).join("、")}${at}，${advice}`;
};
$("reviewLoadButton").addEventListener("click", async () => {
  $("reviewLoadButton").disabled = true;
  $("reviewResult").classList.remove("visible");
  try {
    setStatus("review", true, 30, "正在解析双轨", "建立稳定 ID 与问题索引");
    const source =
        state.reviewSourceText || (await state.reviewSourceFile.text()),
      payload = {
        source_filename: state.reviewSourceName || state.reviewSourceFile.name,
        source_srt: source,
      };
    if (state.reviewChineseText || state.reviewChineseFile) {
      payload.chinese_filename =
        state.reviewChineseName || state.reviewChineseFile.name;
      payload.chinese_srt =
        state.reviewChineseText || (await state.reviewChineseFile.text());
    }
    const result = await json("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.reviewId = result.id;
    state.reviewPairing = result.pairing || null;
    state.reviewTracks.source = result.tracks.source.segments;
    state.reviewTracks.chinese = result.tracks.chinese
      ? result.tracks.chinese.segments
      : [];
    state.reviewTrack = "source";
    state.reviewIndices = { source: 0, chinese: 0 };
    state.reviewHistory = [];
    $("reviewWorkspace").classList.add("active");
    render();
    markReviewSaved();
    setStatus(
      "review",
      true,
      100,
      "审校台已载入",
      pairingText(result.pairing, "双轨结构可以直接配对", "可在审校台调整结构"),
    );
  } catch (error) {
    setStatus("review", true, 0, "载入失败", error.message, true);
  } finally {
    updateButtons();
  }
});
document.querySelectorAll(".track-tab").forEach((button) =>
  button.addEventListener("click", () => {
    if (button.disabled) return;
    commitCurrent();
    state.reviewTrack = button.dataset.track;
    render();
  }),
);
$("reviewSearch").addEventListener("input", renderList);
$("reviewFilter").addEventListener("change", renderList);
$("reviewPrevButton").addEventListener("click", () =>
  openSegment(Math.max(0, currentIndex() - 1)),
);
$("reviewNextButton").addEventListener("click", () =>
  openSegment(Math.min(currentSegments().length - 1, currentIndex() + 1)),
);
$("reviewMarkButton").addEventListener("click", () => {
  commitCurrent(true);
  state.reviewIndices[state.reviewTrack] = Math.min(
    currentSegments().length - 1,
    currentIndex() + 1,
  );
  render();
});
$("reviewDeleteButton").addEventListener("click", () => {
  if (confirm("删除当前轨的这条字幕？可使用“撤销结构操作”恢复。"))
    deleteCurrent(false);
});
$("reviewDeleteBothButton").addEventListener("click", () => {
  if (confirm("同步删除原文轨和中文轨的同序字幕？")) deleteCurrent(true);
});
$("reviewAddButton").addEventListener("click", addAfter);
$("reviewSplitButton").addEventListener("click", splitCurrent);
$("reviewMergeButton").addEventListener("click", () => {
  if (confirm("把当前字幕与下一条合并？")) mergeNext();
});
$("reviewUndoButton").addEventListener("click", undoStructure);
$("reviewSaveButton").addEventListener("click", async () => {
  commitCurrent();
  $("reviewSaveButton").disabled = true;
  $("reviewResult").classList.remove("visible");
  try {
    setStatus(
      "review",
      true,
      70,
      "正在保存双轨修订版",
      "校验 ID、文本和时间范围",
    );
    const payload = { tracks: { source: tracks().source } };
    if (tracks().chinese.length) payload.tracks.chinese = tracks().chinese;
    const result = await json(`/api/review/${state.reviewId}/save`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.reviewTracks.source = result.tracks.source.segments;
    state.sourceText = result.tracks.source.srt;
    state.sourceName = result.tracks.source.download_name;
    state.sourceFile = null;
    $("sourceLabel").textContent = result.tracks.source.download_name;
    $("sourceMeta").textContent = "来自审校台 · 原文修订版";
    $("reviewSourceDownload").href = result.tracks.source.download_url;
    $("reviewSourceDownload").download = result.tracks.source.download_name;
    const chinese = result.tracks.chinese;
    $("reviewChineseDownload").style.display = chinese ? "inline" : "none";
    if (chinese) {
      state.reviewTracks.chinese = chinese.segments;
      state.zhText = chinese.srt;
      state.zhName = chinese.download_name;
      state.zhFile = null;
      $("zhLabel").textContent = chinese.download_name;
      $("zhMeta").textContent = "来自审校台 · 中文修订版";
      $("reviewChineseDownload").href = chinese.download_url;
      $("reviewChineseDownload").download = chinese.download_name;
      state.reviewPairMatched = result.pairing.matched;
      state.reviewPairing = result.pairing;
    } else state.reviewPairMatched = null;
    render();
    markReviewSaved();
    $("reviewResultText").innerHTML =
      `<strong>修订版已保存</strong><br>${pairingText(result.pairing, "双轨时间与数量一致，已接入双语 ASS", "请继续调整后再生成 ASS")}`;
    $("reviewResult").classList.add("visible");
    setStatus("review", true, 100, "修订版已保存", "原始 SRT 保持不变");
    render();
  } catch (error) {
    setStatus("review", true, 0, "保存失败", error.message, true);
  } finally {
    $("reviewSaveButton").disabled = false;
    updateButtons();
  }
});

$("assButton").addEventListener("click", async () => {
  $("assButton").disabled = true;
  $("assResult").classList.remove("visible");
  try {
    setStatus("ass", true, 35, "正在读取双轨", "原文轨时间轴将作为唯一时间源");
    const source = state.sourceText || (await state.sourceFile.text()),
      zh = state.zhText || (await state.zhFile.text());
    const result = await json("/api/ass", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_filename: state.sourceName || state.sourceFile.name,
        source_srt: source,
        translation_filename: state.zhName || state.zhFile.name,
        translation_srt: zh,
      }),
    });
    state.assId = result.id;
    state.assFile = null;
    $("burnAssLabel").textContent = result.download_name;
    $("burnAssMeta").textContent = "来自第 04 步";
    if (result.preview_url) {
      $("assPreview").classList.remove("error");
      $("assStylePreview").src = `${result.preview_url}?v=${Date.now()}`;
    } else if (result.preview_error) {
      $("assPreview").classList.add("error");
      $("assPreviewError").textContent = result.preview_error;
    }
    setStatus(
      "ass",
      true,
      100,
      "双语 ASS 已生成",
      `${result.segment_count} 个字幕段`,
    );
    $("assDownload").href = result.download_url;
    $("assDownload").download = result.download_name;
    $("assResultText").innerHTML =
      `<strong>${result.segment_count} 段已配对</strong><br>中文在上 · 原文在下 · libass 真实预览`;
    $("assResult").classList.add("visible");
  } catch (error) {
    setStatus("ass", true, 0, "ASS 生成失败", error.message, true);
  } finally {
    updateButtons();
  }
});
$("burnButton").addEventListener("click", async () => {
  $("burnButton").disabled = true;
  $("burnResult").classList.remove("visible");
  try {
    let assId = state.assId;
    if (state.assFile) {
      setStatus("burn", true, 0, "正在接收 ASS", state.assFile.name);
      const q = new URLSearchParams({ filename: state.assFile.name });
      assId = (
        await upload(`/api/ass-assets?${q}`, state.assFile, (p) =>
          setStatus(
            "burn",
            true,
            Math.round(p * 0.05),
            "正在接收 ASS",
            `${p}%`,
          ),
        )
      ).id;
    }
    const profile = document.querySelector(
        'input[name="profile"]:checked',
      ).value,
      q = new URLSearchParams({
        filename: state.burnVideo.name,
        ass_id: assId,
        profile,
      });
    setStatus("burn", true, 0, "正在接收视频", state.burnVideo.name);
    const queued = await upload(`/api/burn?${q}`, state.burnVideo, (p) =>
      setStatus(
        "burn",
        true,
        Math.min(8, Math.round(p * 0.08)),
        "正在接收视频",
        `${p}%`,
      ),
    );
    const job = await poll("burn", queued.id, "burn");
    $("burnDownload").href = job.download_url;
    $("burnDownload").download = job.download_name;
    $("burnResultText").innerHTML =
      `<strong>双语视频已完成</strong><br>${profile === "hevc-source" ? "HEVC 原始分辨率" : "HEVC 1080p"} · VideoToolbox 硬件编码 · 保留原帧率`;
    (job.warnings || []).forEach((w) => {
      const note = document.createElement("span");
      note.className = "result-warning";
      note.textContent = `注意：${w}`;
      $("burnResultText").append(document.createElement("br"), note);
    });
    $("burnResult").classList.add("visible");
  } catch (error) {
    setStatus("burn", true, 0, "视频压制失败", error.message, true);
  } finally {
    updateButtons();
  }
});
$("assStylePreview").addEventListener("load", () => {
  $("assPreview").classList.remove("error");
});
$("assStylePreview").addEventListener("error", () => {
  $("assPreview").classList.add("error");
});
environment();
updateButtons();
$("reviewChineseDownload").style.display = "none";
