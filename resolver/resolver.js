#!/usr/bin/env node
/**
 * 本地短视频解析核心（抖音 + 小红书）。
 *
 * 用无头 Edge / Chrome + CDP 打开分享链接，从页面自己发出的请求 / SSR 状态里取直链：
 *
 *  - 抖音：aweme/v1/web/aweme/detail 被 Argus 风控保护，纯 HTTP 客户端（yt-dlp / curl）
 *    即使带浏览器 cookie 也会被 403 / 空响应拦掉；页面自身的请求带 secsdk 签名，
 *    所以从页面里抓 aweme_detail 最稳。抖音还会按 IP 信誉拦截机房 IP，必须住宅网络。
 *
 *  - 小红书：PC 网页端直接 HTTP 请求会被重定向到 /login，但真实浏览器打开分享链接
 *    （带 xsec_token）时 SSR 的 window.__INITIAL_STATE__ 里就有笔记数据，
 *    note.video.media.stream.*[*].masterUrl 即无水印直链。
 *
 * 独立测试：
 *   node resolver.js once "https://v.douyin.com/xxxx/"
 *   node resolver.js once "https://xhslink.cn/o/xxxx"
 */

'use strict';

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const DEFAULT_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function log(...args) {
  console.log(new Date().toISOString(), ...args);
}

function hostOf(url) {
  try {
    return new URL(String(url)).hostname.toLowerCase();
  } catch {
    return '';
  }
}

/** 判断链接属于哪个平台。 */
function detectPlatform(url) {
  const host = hostOf(url);
  if (!host) return null;
  if (host === 'douyin.com' || host.endsWith('.douyin.com') || host.endsWith('iesdouyin.com')) return 'douyin';
  if (host === 'xiaohongshu.com' || host.endsWith('.xiaohongshu.com') || /(^|\.)xhslink\.(com|cn)$/.test(host)) {
    return 'xiaohongshu';
  }
  if (/(^|\.)tiktok\.com$/.test(host)) return 'tiktok';
  return null;
}

/** 从各种抖音链接里取出数字 aweme_id。 */
function extractAwemeId(url) {
  const patterns = [
    /douyin\.com\/video\/(\d{6,})/i,
    /iesdouyin\.com\/share\/video\/(\d{6,})/i,
    /modal_id=(\d{6,})/i,
    /aweme_id=(\d{6,})/i,
    /(?:^|\D)(\d{17,20})(?:\D|$)/,
  ];
  for (const re of patterns) {
    const match = String(url || '').match(re);
    if (match) return match[1];
  }
  return null;
}

/** v.douyin.com 短链：普通 HTTP 302 就能展开（这一步不需要 cookie）。 */
async function expandShortLink(url, userAgent) {
  let current = url;
  for (let hop = 0; hop < 5; hop++) {
    if (extractAwemeId(current)) return { url: current, awemeId: extractAwemeId(current) };
    let response;
    try {
      response = await fetch(current, {
        redirect: 'manual',
        headers: { 'User-Agent': userAgent, 'Accept-Language': 'zh-CN,zh;q=0.9' },
      });
    } catch (error) {
      return { url: current, awemeId: null, error: `短链展开失败：${error.message}` };
    }
    const location = response.headers.get('location');
    if (!location) return { url: current, awemeId: extractAwemeId(current) };
    current = new URL(location, current).toString();
  }
  return { url: current, awemeId: extractAwemeId(current) };
}

// ---------------------------------------------------------------- CDP 浏览器

class Browser {
  constructor(options = {}) {
    this.binary = options.binary;
    this.profileDir = options.profileDir || path.join(__dirname, '.browser-profile');
    this.proxy = options.proxy || '';
    this.userAgent = options.userAgent || DEFAULT_UA;
    this.callTimeoutMs = options.callTimeoutMs || 20000;
    this.proc = null;
    this.ws = null;
    this.sessionId = null;
    this.targetId = null;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Set();
    this.warmedUp = false;
    this.port = 0;
  }

  async start() {
    if (this.proc) return;
    try {
      await this._startOnce();
    } catch (error) {
      // 常见原因：上一次浏览器进程被强杀 / 解析器重启后，旧 msedge 仍占着同一个
      // user-data-dir，Windows 下新进程会立刻退出（表现为「浏览器进程在启动阶段就退出了」）。
      // 退一步换一个全新的 profile 目录重试一次，让解析器自己恢复，不必人工干预。
      const alt = `${this.profileDir}-alt-${Date.now()}`;
      log(`浏览器启动失败（${error.message}），改用新 profile 目录重试：${alt}`);
      this.profileDir = alt;
      await this._startOnce();
    }
  }

  async _startOnce() {
    fs.mkdirSync(this.profileDir, { recursive: true });
    this.port = 9222 + Math.floor(Math.random() * 600);
    const args = [
      ...(this.proxy ? [`--proxy-server=${this.proxy}`] : []),
      '--headless=new',
      `--remote-debugging-port=${this.port}`,
      `--user-data-dir=${this.profileDir}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-gpu',
      '--mute-audio',
      '--disable-extensions',
      '--disable-sync',
      '--disable-blink-features=AutomationControlled',
      '--autoplay-policy=no-user-gesture-required',
      '--lang=zh-CN',
      '--window-size=1280,800',
      'about:blank',
    ];
    log('启动浏览器:', this.binary);
    this.proc = spawn(this.binary, args, { stdio: 'ignore' });
    this.proc.on('exit', (code, signal) => {
      log(`浏览器已退出 code=${code} signal=${signal}`);
      this.proc = null;
      this.ws = null;
      this.sessionId = null;
      this.warmedUp = false;
    });

    let version = null;
    for (let i = 0; i < 80; i++) {
      try {
        const response = await fetch(`http://127.0.0.1:${this.port}/json/version`);
        if (response.ok) {
          version = await response.json();
          break;
        }
      } catch {}
      if (!this.proc) throw new Error('浏览器进程在启动阶段就退出了');
      await sleep(250);
    }
    if (!version) throw new Error('无法连接浏览器的调试端口');

    this.ws = await new Promise((resolve, reject) => {
      const socket = new WebSocket(version.webSocketDebuggerUrl);
      socket.onopen = () => resolve(socket);
      socket.onerror = () => reject(new Error('调试端口 WebSocket 连接失败'));
    });
    this.ws.onmessage = (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) reject(new Error(JSON.stringify(message.error)));
        else resolve(message.result);
        return;
      }
      for (const listener of this.listeners) listener(message);
    };
    log('浏览器就绪，调试端口', this.port);
  }

  send(method, params = {}, sessionId = this.sessionId) {
    // 每个 CDP 调用都必须带超时：浏览器一旦卡住，没有超时的 Promise 会永远挂起，
    // 解析器就再也不发心跳（群里表现为「本地解析器未连接」）。
    const timeoutMs = this.callTimeoutMs || 20000;
    return new Promise((resolve, reject) => {
      const id = this.nextId++;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP ${method} 调用超时（${timeoutMs}ms）`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: (error) => {
          clearTimeout(timer);
          reject(error);
        },
      });
      try {
        this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new Error(`CDP ${method} 发送失败：${error.message}`));
      }
    });
  }

  onMessage(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  async stop() {
    try {
      if (this.ws) this.ws.close();
    } catch {}
    try {
      if (this.proc) this.proc.kill();
    } catch {}
    this.proc = null;
    this.ws = null;
    this.sessionId = null;
    this.warmedUp = false;
  }

  async newPage() {
    const { targetId } = await this.send('Target.createTarget', { url: 'about:blank' }, null);
    const { sessionId } = await this.send('Target.attachToTarget', { targetId, flatten: true }, null);
    this.targetId = targetId;
    this.sessionId = sessionId;
    return sessionId;
  }

  async closePage(targetId) {
    try {
      await this.send('Target.closeTarget', { targetId }, null);
    } catch {}
  }

  /** 打开页面并返回会话；调用方负责 closePage。 */
  async openPage(url) {
    const sessionId = await this.newPage();
    const targetId = this.targetId;
    await this.send('Page.enable', {}, sessionId);
    await this.send('Runtime.enable', {}, sessionId);
    await this.send('Network.enable', {}, sessionId);
    await this.send(
      'Network.setUserAgentOverride',
      { userAgent: this.userAgent, acceptLanguage: 'zh-CN,zh;q=0.9' },
      sessionId
    );
    if (url) await this.send('Page.navigate', { url }, sessionId);
    return { sessionId, targetId };
  }

  async evaluate(sessionId, expression) {
    const result = await this.send('Runtime.evaluate', { expression, returnByValue: true }, sessionId);
    if (result.exceptionDetails) throw new Error('页面脚本执行失败');
    return result.result ? result.result.value : undefined;
  }

  async currentUrl(sessionId) {
    try {
      return (await this.evaluate(sessionId, 'location.href')) || '';
    } catch {
      return '';
    }
  }
}

// ---------------------------------------------------------------- 抖音

// 在页面里挂钩 fetch / XHR，把 aweme/detail 的响应留一份给外面读。
const DOUYIN_CAPTURE_HOOK = `(() => {
  const store = [];
  Object.defineProperty(window, '__dshDetail', { value: store, configurable: true });
  const keep = (url, text) => {
    try {
      if (!text || !/aweme\\/detail/.test(String(url))) return;
      const json = JSON.parse(text);
      const detail = json.aweme_detail || (Array.isArray(json.aweme_details) ? json.aweme_details[0] : null);
      if (detail) store.push(detail);
    } catch (e) {}
  };
  const origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (...args) {
      const request = args[0];
      const url = typeof request === 'string' ? request : (request && request.url) || '';
      const result = origFetch.apply(this, args);
      try {
        result.then((res) => { try { res.clone().text().then((t) => keep(url, t)).catch(() => {}); } catch (e) {} });
      } catch (e) {}
      return result;
    };
  }
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__dshUrl = url;
    return open.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener('load', () => { try { keep(this.__dshUrl, this.responseText); } catch (e) {} });
    return send.apply(this, args);
  };
})();`;

function pickDouyinVideos(detail) {
  const video = detail.video || {};
  const videos = [];
  const seen = new Set();
  const push = (label, url, size, width, height, quality) => {
    if (!url || seen.has(url)) return;
    seen.add(url);
    videos.push({
      label,
      url,
      size: size ?? null,
      width: width ?? null,
      height: height ?? null,
      quality: quality ?? null,
    });
  };

  const play = video.play_addr || {};
  for (const [index, url] of (play.url_list || []).entries()) {
    push(`default-${index}`, url, play.data_size, play.width, play.height, null);
  }
  for (const rate of video.bit_rate || []) {
    const addr = rate.play_addr || {};
    const urls = addr.url_list || [];
    if (!urls.length) continue;
    push(rate.gear_name || `gear-${rate.quality_type}`, urls[0], addr.data_size, addr.width, addr.height, rate.quality_type);
  }

  return {
    videos,
    cover: (video.cover?.url_list || video.origin_cover?.url_list || [])[0] || null,
    images: Array.isArray(detail.images) ? detail.images.length : 0,
    // 图文作品（图集）的图片直链；插件据此改发图片组
    image_list: Array.isArray(detail.images)
      ? detail.images.map((item) => (item && (item.url_list || [])[0]) || null).filter(Boolean)
      : [],
  };
}

async function resolveDouyin(browser, rawUrl, options = {}) {
  const navTimeout = options.navTimeoutMs || 45000;
  const warmupMs = options.warmupMs ?? 8000;
  const started = Date.now();

  const expanded = await expandShortLink(rawUrl, browser.userAgent);
  const awemeId = expanded.awemeId || extractAwemeId(rawUrl);
  // 短链如果展开失败（会被 302 到首页），就直接把原始链接交给浏览器自己跳。
  const targetUrl = awemeId ? `https://www.douyin.com/video/${awemeId}` : expanded.url;

  // 首次使用先访问一次首页，让页面风控 SDK 生成设备 cookie，否则详情接口有时返回空。
  if (!browser.warmedUp) {
    const { sessionId: warmSession, targetId: warmTarget } = await browser.openPage('https://www.douyin.com/');
    try {
      await sleep(warmupMs);
    } finally {
      await browser.closePage(warmTarget);
      void warmSession;
    }
    browser.warmedUp = true;
  }

  const { sessionId, targetId } = await browser.openPage(null);
  const pendingBodies = new Set();
  const bodyResults = [];
  let bodyTask = Promise.resolve();

  const fetchBody = (requestId) => {
    bodyTask = bodyTask.then(async () => {
      for (let attempt = 0; attempt < 6; attempt++) {
        try {
          const body = await browser.send('Network.getResponseBody', { requestId }, sessionId);
          const text = body.base64Encoded ? Buffer.from(body.body, 'base64').toString('utf8') : body.body;
          if (!text) throw new Error('empty body');
          const json = JSON.parse(text);
          const detail = json.aweme_detail || (Array.isArray(json.aweme_details) ? json.aweme_details[0] : null);
          if (detail && detail.aweme_id) bodyResults.push(detail);
          return;
        } catch {
          await sleep(250);
        }
      }
    });
  };

  const unsubscribe = browser.onMessage((message) => {
    const url = message.params?.response?.url || '';
    if (message.method === 'Network.responseReceived' && /aweme\/detail/.test(url)) {
      pendingBodies.add(message.params.requestId);
    }
    if (message.method === 'Network.loadingFinished' && pendingBodies.has(message.params.requestId)) {
      pendingBodies.delete(message.params.requestId);
      fetchBody(message.params.requestId);
    }
  });

  try {
    await browser.send('Page.addScriptToEvaluateOnNewDocument', { source: DOUYIN_CAPTURE_HOOK }, sessionId);
    await browser.send('Page.navigate', { url: targetUrl }, sessionId);

    const deadline = Date.now() + navTimeout;
    let detail = null;
    let landedUrl = targetUrl;
    while (Date.now() < deadline) {
      await sleep(700);
      const current = await browser.currentUrl(sessionId);
      if (current) landedUrl = current;

      try {
        const raw = await browser.evaluate(sessionId, 'JSON.stringify(window.__dshDetail || [])');
        const list = JSON.parse(raw || '[]');
        const found = list.filter((item) => item && item.aweme_id).pop();
        if (found) {
          detail = found;
          break;
        }
      } catch {}

      await bodyTask;
      if (bodyResults.length) {
        detail = bodyResults[bodyResults.length - 1];
        break;
      }
    }

    if (!detail) {
      const landedId = extractAwemeId(landedUrl);
      return {
        ok: false,
        platform: 'douyin',
        aweme_id: landedId || awemeId || null,
        error: `抖音页面没有返回视频详情（当前页面：${String(landedUrl).slice(0, 80)}）。可能触发了风控，稍后重试。`,
      };
    }

    const picked = pickDouyinVideos(detail);
    if (!picked.videos.length) {
      if (picked.image_list.length) {
        // 图文作品：没有视频流但有图片，交给插件按图片组发送
        return {
          ok: true,
          mode: 'images',
          platform: 'douyin',
          aweme_id: String(detail.aweme_id || awemeId || ''),
          title: String(detail.desc || '').trim(),
          author: detail.author?.nickname || null,
          cover: picked.cover || picked.image_list[0],
          images: picked.image_list.length,
          image_list: picked.image_list,
          elapsed_ms: Date.now() - started,
          videos: [],
        };
      }
      return {
        ok: false,
        platform: 'douyin',
        aweme_id: String(detail.aweme_id || awemeId || ''),
        error: picked.images > 0 ? '这是一个图文作品，但没取到图片直链' : '未能取得视频直链',
      };
    }

    return {
      ok: true,
      platform: 'douyin',
      aweme_id: String(detail.aweme_id || awemeId || ''),
      title: String(detail.desc || '').trim(),
      author: detail.author?.nickname || null,
      duration_ms: detail.video?.duration ?? null,
      cover: picked.cover,
      images: picked.images,
      image_list: picked.image_list,
      elapsed_ms: Date.now() - started,
      videos: picked.videos,
    };
  } finally {
    unsubscribe();
    await browser.closePage(targetId);
  }
}

// ---------------------------------------------------------------- 小红书

const XHS_EXTRACT_EXPR = [
  '(() => {',
  '  const out = { found: false, href: location.href.slice(0, 220) };',
  '  const st = window.__INITIAL_STATE__;',
  '  try {',
  '    const map = st && st.note && st.note.noteDetailMap;',
  '    if (map) {',
  '      const holder = Object.values(map)[0] || {};',
  '      const note = holder.note || holder.noteData || holder;',
  '      if (note && (note.noteId || note.id)) {',
  '        out.found = true;',
  '        out.note_id = note.noteId || note.id;',
  '        out.title = (note.title || "").trim();',
  '        out.desc = (note.desc || "").trim().slice(0, 200);',
  '        out.type = note.type || null;',
  '        out.images = Array.isArray(note.imageList) ? note.imageList.length : 0;',
  '        out.cover = (note.imageList && note.imageList[0] &&',
  '          ((note.imageList[0].urlDefault) || (note.imageList[0].urlPre) ||',
  '           (note.imageList[0].infoList && note.imageList[0].infoList[0] && note.imageList[0].infoList[0].url))) || null;',
  '        out.image_list = Array.isArray(note.imageList) ? note.imageList.map((it) =>',
  '          (it && (it.urlDefault || it.urlPre ||',
  '            (it.infoList && it.infoList[0] && it.infoList[0].url))) || null).filter(Boolean) : [];',
  '        const video = note.video || {};',
  '        out.duration_ms = (video.capa && video.capa.duration ? video.capa.duration * 1000 : null);',
  '        const streams = (video.media && video.media.stream) || {};',
  '        const list = [];',
  '        for (const codec of Object.keys(streams)) {',
  '          for (const item of (streams[codec] || [])) {',
  '            if (!item) continue;',
  '            const url = item.masterUrl || (item.backupUrls || [])[0];',
  '            if (!url) continue;',
  '            list.push({',
  '              label: codec + "-" + (item.qualityType || item.videoCodec || list.length),',
  '              url: url,',
  '              size: item.size || null,',
  '              width: item.width || null,',
  '              height: item.height || null,',
  '              quality: item.qualityType || item.videoCodec || codec,',
  '              codec: codec,',
  '            });',
  '          }',
  '        }',
  '        out.streams = list;',
  '      }',
  '    }',
  '  } catch (e) { out.error = String(e).slice(0, 160); }',
  '  return JSON.stringify(out);',
  '})()',
].join('\n');

function pickXhsVideos(streams) {
  // 优先 h264（QQ 兼容性最好），其余按体积从大到小兜底。
  const order = { h264: 0, h265: 1, av1: 2 };
  return [...(streams || [])].sort((a, b) => {
    const oa = order[a.codec] ?? 9;
    const ob = order[b.codec] ?? 9;
    if (oa !== ob) return oa - ob;
    return (b.size || 0) - (a.size || 0);
  });
}

async function resolveXiaohongshu(browser, rawUrl, options = {}) {
  const navTimeout = options.navTimeoutMs || 45000;
  const started = Date.now();
  const { sessionId, targetId } = await browser.openPage(null);

  try {
    await browser.send('Page.navigate', { url: rawUrl }, sessionId);
    const deadline = Date.now() + navTimeout;
    let info = null;
    let landedUrl = rawUrl;

    while (Date.now() < deadline) {
      await sleep(800);
      const current = await browser.currentUrl(sessionId);
      if (current) landedUrl = current;
      try {
        const raw = await browser.evaluate(sessionId, XHS_EXTRACT_EXPR);
        const parsed = JSON.parse(raw || '{}');
        if (parsed.found) {
          info = parsed;
          break;
        }
      } catch {}
    }

    if (!info) {
      return {
        ok: false,
        platform: 'xiaohongshu',
        error: `小红书页面没有返回笔记数据（当前页面：${String(landedUrl).slice(0, 80)}）。分享链接一般带 xsec_token，请用 App 里的「复制链接」再试。`,
      };
    }

    const videos = pickXhsVideos(info.streams);
    const imageList = Array.isArray(info.image_list) ? info.image_list : [];
    if (!videos.length) {
      if (imageList.length) {
        // 图文笔记：没有视频流但有图片，交给插件按图片组发送
        return {
          ok: true,
          mode: 'images',
          platform: 'xiaohongshu',
          note_id: info.note_id || null,
          aweme_id: info.note_id || null,
          title: info.title || info.desc || '',
          cover: info.cover || imageList[0],
          images: imageList.length,
          image_list: imageList,
          elapsed_ms: Date.now() - started,
          videos: [],
        };
      }
      return {
        ok: false,
        platform: 'xiaohongshu',
        note_id: info.note_id || null,
        title: info.title || info.desc || '',
        error: info.images > 0 ? '这是一篇图文笔记，但没取到图片直链' : '未能取得视频直链',
      };
    }

    return {
      ok: true,
      platform: 'xiaohongshu',
      note_id: info.note_id || null,
      aweme_id: info.note_id || null,
      title: info.title || info.desc || '',
      author: null,
      duration_ms: info.duration_ms ?? null,
      cover: info.cover || null,
      images: info.images || 0,
      image_list: imageList,
      elapsed_ms: Date.now() - started,
      videos,
    };
  } finally {
    await browser.closePage(targetId);
  }
}

// ---------------------------------------------------------------- TikTok

/**
 * TikTok 走独立通道：中国大陆直连 TikTok 会被 DNS 污染 + 连接超时，
 * 云服务器更是完全不可达，所以必须在**本机**通过代理（Clash/V2Ray 等）下载，
 * 再把本机文件路径交给同机的 NapCat 上传。
 *
 * 这里复用 yt-dlp 的 TikTok 提取器（自带的独立 exe，不需要装 Python）：
 *   bin/yt-dlp.exe --proxy <proxy> -f "b[ext=mp4]/b" -o downloads/tt_%(id)s.%(ext)s <url>
 */
function resolveYtDlpBinary(configured) {
  const candidates = [
    configured,
    path.join(__dirname, 'bin', process.platform === 'win32' ? 'yt-dlp.exe' : 'yt-dlp'),
    path.join(__dirname, 'yt-dlp.exe'),
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return configured || 'yt-dlp';
}

function pruneDownloads(dir, keepHours) {
  try {
    const deadline = Date.now() - keepHours * 3600 * 1000;
    for (const name of fs.readdirSync(dir)) {
      if (!/\.(mp4|mov|webm|mkv)$/i.test(name)) continue;
      const full = path.join(dir, name);
      const stat = fs.statSync(full);
      if (stat.mtimeMs < deadline) fs.unlinkSync(full);
    }
  } catch {}
}

function runYtDlp(binary, args, timeoutMs) {
  return new Promise((resolve) => {
    const child = spawn(binary, args, { windowsHide: true });
    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {}
    }, timeoutMs);
    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString('utf8');
    });
    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString('utf8');
    });
    child.on('error', (error) => {
      clearTimeout(timer);
      resolve({ code: -1, stdout, stderr: `${stderr}\n${error.message}` });
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
  });
}

async function downloadTiktok(config, rawUrl) {
  const started = Date.now();
  const binary = resolveYtDlpBinary(config.ytDlpPath);
  const downloadDir = config.downloadDir || path.join(__dirname, 'downloads');
  fs.mkdirSync(downloadDir, { recursive: true });
  pruneDownloads(downloadDir, config.keepHours ?? 6);

  const proxy = config.tiktokProxy || config.proxy || '';
  const maxMb = Number(config.maxDownloadMb || 120);
  const outputTemplate = path.join(downloadDir, 'tt_%(id)s.%(ext)s');

  const buildArgs = (useProxy) => {
    const list = [
      '--no-warnings',
      '--no-progress',
      '--no-playlist',
      '--no-simulate',
      '--restrict-filenames',
      '--max-filesize',
      `${maxMb}M`,
      '-f',
      config.tiktokFormat || 'b[ext=mp4]/b',
      '-o',
      outputTemplate,
      '--print',
      'after_move:%(id)s\t%(title)s\t%(duration)s\t%(filesize)s\t%(filepath)s',
    ];
    if (useProxy) list.push('--proxy', useProxy);
    list.push(rawUrl);
    return list;
  };

  // TikTok 偶尔会因为限流 / 指纹校验返回 403，属于瞬时错误：
  // 依次尝试「代理 → 代理（等 4 秒）→ 直连」，全部失败才报错。
  const plans = proxy
    ? [{ proxy, wait: 0 }, { proxy, wait: 4000 }, { proxy: '', wait: 4000 }]
    : [{ proxy: '', wait: 0 }, { proxy: '', wait: 4000 }];

  let stdout = '';
  let lastDetail = '';
  for (const [index, plan] of plans.entries()) {
    if (plan.wait) await sleep(plan.wait);
    log(`TikTok 下载（第 ${index + 1}/${plans.length} 次）${plan.proxy ? ` proxy=${plan.proxy}` : ' 直连'}`);
    const result = await runYtDlp(
      binary,
      buildArgs(plan.proxy),
      Number(config.downloadTimeoutMs || 300000),
    );
    if (result.code === 0) {
      stdout = result.stdout;
      break;
    }
    lastDetail =
      (result.stderr || result.stdout).trim().split('\n').filter(Boolean).slice(-1)[0] ||
      'yt-dlp 执行失败';
    log(`TikTok 第 ${index + 1} 次失败：${lastDetail.slice(0, 160)}`);
  }

  if (!stdout) {
    const hint = proxy
      ? '（已自动重试 3 次，仍是瞬时错误，请稍后再发一次）'
      : '（当前未配置 tiktokProxy，TikTok 通常需要代理）';
    return {
      ok: false,
      platform: 'tiktok',
      error: `TikTok 下载失败${hint}：${lastDetail.slice(0, 200)}`,
    };
  }

  const last = stdout.trim().split('\n').filter(Boolean).slice(-1)[0] || '';
  const [mediaId, title, duration, filesize, filePath] = last.split('\t');
  if (!filePath || !fs.existsSync(filePath)) {
    return { ok: false, platform: 'tiktok', error: `TikTok 下载完成但找不到文件：${last.slice(0, 160)}` };
  }
  const stat = fs.statSync(filePath);

  return {
    ok: true,
    platform: 'tiktok',
    mode: 'local_file',
    local_path: filePath,
    media_id: mediaId || null,
    aweme_id: mediaId || null,
    title: (title || '').trim(),
    duration_ms: duration && Number(duration) ? Number(duration) * 1000 : null,
    size: stat.size || Number(filesize) || null,
    elapsed_ms: Date.now() - started,
    videos: [],
  };
}

// ---------------------------------------------------------------- 统一入口

/** 按链接域名分派到对应平台解析。抖音/小红书返回直链，TikTok 直接在本机下好文件。 */
async function resolveMedia(browser, url, options = {}) {
  const platform = detectPlatform(url);
  if (platform === 'douyin') return resolveDouyin(browser, url, options);
  if (platform === 'xiaohongshu') return resolveXiaohongshu(browser, url, options);
  if (platform === 'tiktok') return downloadTiktok(options.config || {}, url);
  return { ok: false, error: `不支持的链接：${url}` };
}

module.exports = {
  Browser,
  resolveMedia,
  resolveDouyin,
  resolveXiaohongshu,
  downloadTiktok,
  detectPlatform,
  extractAwemeId,
  expandShortLink,
  DEFAULT_UA,
};

if (require.main === module) {
  (async () => {
    const mode = process.argv[2];
    if (mode !== 'once' || !process.argv[3]) {
      console.error('用法: node resolver.js once "<抖音/小红书链接>" [浏览器路径]');
      process.exit(2);
    }
    const binary = process.argv[4] || process.env.RESOLVER_BROWSER || 'msedge';
    const browser = new Browser({ binary, profileDir: path.join(os.tmpdir(), 'dsh-media-manual') });
    let code = 1;
    try {
      await browser.start();
      const result = await resolveMedia(browser, process.argv[3], {
        config: {
          tiktokProxy: process.env.TIKTOK_PROXY || '',
          ytDlpPath: process.env.YTDLP_PATH || '',
          downloadDir: process.env.DOWNLOAD_DIR || path.join(__dirname, 'downloads'),
        },
      });
      console.log(JSON.stringify(result, null, 2));
      code = result.ok ? 0 : 1;
    } catch (error) {
      console.error('解析失败:', error.message);
    } finally {
      await browser.stop();
    }
    process.exit(code);
  })();
}
