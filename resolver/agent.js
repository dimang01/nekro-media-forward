#!/usr/bin/env node
/**
 * 本地短视频解析器（轮询代理）— 支持抖音 / 小红书。
 *
 * 云服务器上的 video_forward 插件会把待解析链接排成任务；本进程在住宅网络里轮询领取
 * 任务，用无头 Edge 解析出无水印直链，再回传给服务器。服务器负责下载并以合并转发发到
 * QQ，所以本机不需要暴露任何端口。
 *
 * 用法：
 *   node agent.js                 # 前台运行（调试用）
 *   start.bat                     # 双击运行
 */

'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { Browser, resolveMedia, detectPlatform } = require('./resolver');

const SCRIPT_DIR = __dirname;
const CONFIG_PATH = path.join(SCRIPT_DIR, 'config.json');
const LOG_PATH = path.join(SCRIPT_DIR, 'resolver.log');
const MAX_LOG_BYTES = 2 * 1024 * 1024;

const PLATFORM_NAMES = { douyin: '抖音', xiaohongshu: '小红书', tiktok: 'TikTok' };

function loadConfig() {
  if (!fs.existsSync(CONFIG_PATH)) {
    console.error(`缺少配置文件：${CONFIG_PATH}`);
    console.error('请复制 config.example.json 为 config.json 并填写 server / token。');
    process.exit(2);
  }
  const config = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
  if (!config.server) throw new Error('config.json 里必须填写 server（NekroAgent 公网地址）');
  if (!config.token) throw new Error('config.json 里必须填写 token（与插件配置里的密钥一致）');
  return {
    server: String(config.server).replace(/\/+$/, ''),
    token: String(config.token),
    pluginPath: config.pluginPath || '/plugins/local_video.video_forward',
    browser: config.browser || findBrowser(),
    proxy: config.proxy || '',
    tiktokProxy: config.tiktokProxy || config.proxy || '',
    ytDlpPath: config.ytDlpPath || '',
    downloadDir: config.downloadDir || path.join(SCRIPT_DIR, 'downloads'),
    maxDownloadMb: Number(config.maxDownloadMb || 120),
    keepHours: Number(config.keepHours || 6),
    downloadTimeoutMs: Number(config.downloadTimeoutMs || 300000),
    callTimeoutMs: Number(config.callTimeoutMs || 20000),
    jobTimeoutMs: Number(config.jobTimeoutMs || 120000),
    maxUploadMb: Number(config.maxUploadMb || config.maxDownloadMb || 120),
    uploadTimeoutMs: Number(config.uploadTimeoutMs || 900000),
    pollWaitSeconds: Number(config.pollWaitSeconds || 20),
    warmupMs: Number(config.warmupMs || 8000),
    navTimeoutMs: Number(config.navTimeoutMs || 45000),
    retryDelayMs: Number(config.retryDelayMs || 5000),
  };
}

function findBrowser() {
  const candidates = [
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return 'msedge';
}

function rotateLog() {
  try {
    if (fs.existsSync(LOG_PATH) && fs.statSync(LOG_PATH).size > MAX_LOG_BYTES) {
      fs.renameSync(LOG_PATH, `${LOG_PATH}.1`);
    }
  } catch {}
}

function log(...args) {
  const line = `${new Date().toISOString()} ${args
    .map((item) => (typeof item === 'string' ? item : JSON.stringify(item)))
    .join(' ')}`;
  console.log(line);
  try {
    rotateLog();
    fs.appendFileSync(LOG_PATH, `${line}\n`);
  } catch {}
}

async function httpJson(url, options = {}, timeoutMs = 60000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const text = await response.text();
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${text.slice(0, 200)}`);
    }
    return text ? JSON.parse(text) : {};
  } finally {
    clearTimeout(timer);
  }
}

class Agent {
  constructor(config) {
    this.config = config;
    this.browser = new Browser({
      binary: config.browser,
      profileDir: path.join(SCRIPT_DIR, '.browser-profile'),
      proxy: config.proxy,
      callTimeoutMs: config.callTimeoutMs,
    });
    this.running = true;
    this.consecutiveFailures = 0;
    this.currentJob = null; // { id, url, platform, startedAt }
  }

  endpoint(suffix) {
    const { server, pluginPath, token } = this.config;
    const separator = suffix.includes('?') ? '&' : '?';
    return `${server}${pluginPath}${suffix}${separator}token=${encodeURIComponent(token)}`;
  }

  async ensureBrowser() {
    if (!this.browser.proc) {
      await this.browser.start();
    }
  }

  async restartBrowser() {
    await this.browser.stop().catch(() => {});
    await this.browser.start();
  }

  /**
   * 卡死看门狗。
   *
   * 浏览器/网络异常时，某个 await 可能永远不返回（即使加了 CDP 超时也可能卡在
   * 页面内的 fetch 上）。这里用定时器兜底：任务超时就直接回报失败并重建浏览器，
   * 保证「解析器在线」这个心跳不会被一个坏任务拖死。
   */
  startWatchdog() {
    const limit = this.config.jobTimeoutMs;
    setInterval(() => {
      const job = this.currentJob;
      if (!job) return;
      const elapsed = Date.now() - job.startedAt;
      if (elapsed < limit) return;
      log(`任务 ${job.id} 超过 ${(limit / 1000).toFixed(0)}s 仍未结束，强制回收并回报失败`);
      this.currentJob = null;
      this.report({
        id: job.id,
        ok: false,
        platform: job.platform,
        error: `解析超时（超过 ${(limit / 1000).toFixed(0)} 秒，解析器已自动恢复，请重发一次链接）`,
      }).catch((error) => log('回报超时任务失败:', error.message));
      if (job.platform !== 'tiktok') {
        this.restartBrowser().catch((error) => log('重建浏览器失败:', error.message));
      }
    }, 15000).unref?.();
  }

  async poll() {
    return httpJson(this.endpoint(`/resolver/poll?wait=${this.config.pollWaitSeconds}`), {}, 40000);
  }

  async report(payload) {
    return httpJson(
      this.endpoint('/resolver/result'),
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
      30000
    );
  }

  /**
   * 把本机下好的文件推给服务器（流式上传，不占内存）。
   *
   * QQ/NapCat 上云后不再与本解析器同机，插件拿到的 local_path 在服务器上不存在，
   * 所以必须先上传；插件随后用 /media 临时链接交付给 NapCat。
   */
  async upload(jobId, filePath, fileName) {
    const stat = fs.statSync(filePath);
    if (!stat.isFile() || stat.size <= 0) throw new Error('本机文件不存在或为空');
    const limit = this.config.maxUploadMb * 1048576;
    if (stat.size > limit) {
      throw new Error(
        `文件 ${(stat.size / 1048576).toFixed(1)}MB 超过上传上限 ${this.config.maxUploadMb}MB`
      );
    }
    const url = this.endpoint(
      `/resolver/upload?job=${encodeURIComponent(jobId)}&name=${encodeURIComponent(fileName)}`
    );
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.config.uploadTimeoutMs);
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/octet-stream',
          'Content-Length': String(stat.size),
        },
        body: fs.createReadStream(filePath),
        duplex: 'half',
        signal: controller.signal,
      });
      const text = await response.text();
      let data = null;
      try { data = JSON.parse(text); } catch {}
      if (!response.ok || !data || data.ok !== true) {
        throw new Error(`HTTP ${response.status} ${text.slice(0, 160)}`);
      }
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  async handleJob(job) {
    const platform = detectPlatform(job.url);
    const platformName = PLATFORM_NAMES[platform] || platform || '未知平台';
    log(`收到任务 ${job.id}（${platformName}）：${job.url}`);
    const started = Date.now();
    this.currentJob = { id: job.id, url: job.url, platform, startedAt: started };
    let result;
    try {
      // TikTok 不需要浏览器：直接由 yt-dlp 走代理下到本机
      if (platform === 'tiktok') {
        result = await resolveMedia(this.browser, job.url, { config: this.config });
      } else {
        await this.ensureBrowser();
        result = await resolveMedia(this.browser, job.url, {
          warmupMs: this.config.warmupMs,
          navTimeoutMs: this.config.navTimeoutMs,
        });
      }
    } catch (error) {
      result = { ok: false, error: `解析异常：${error.message}` };
      // 浏览器状态异常时重建，避免后续任务全部失败
      if (platform !== 'tiktok') {
        await this.restartBrowser().catch((err) => log('重建浏览器失败:', err.message));
      }
    }

    // 看门狗已经回收过这个任务（回报过失败了）就不再重复回报
    const active = this.currentJob && this.currentJob.id === job.id;
    this.currentJob = null;
    if (!active) {
      log(`任务 ${job.id} 已被看门狗回收，丢弃迟到的结果`);
      return;
    }

    const payload = { id: job.id, ...result };
    if (result.ok && result.mode === 'local_file' && result.local_path) {
      // NapCat 可能已不在本机（QQ 上云）：先上传，插件才会用服务器侧文件发送
      try {
        const up = await this.upload(
          job.id,
          result.local_path,
          path.basename(result.local_path) || 'video.mp4'
        );
        log(
          `已上传到服务器 ${job.id}：${(Number(up.size || 0) / 1048576).toFixed(1)}MB`
        );
      } catch (error) {
        log(`上传到服务器失败（仍按本机路径回传，同机 NapCat 不受影响）：${error.message}`);
      }
    }
    if (result.ok && result.mode === 'local_file') {
      log(
        `下载成功 ${job.id}（${platformName}）：${(result.title || '').slice(0, 40)} ` +
          `文件=${result.local_path} 体积=${result.size ? (result.size / 1048576).toFixed(1) + 'MB' : '未知'} ` +
          `耗时=${((Date.now() - started) / 1000).toFixed(1)}s`
      );
    } else if (result.ok) {
      const best = (result.videos || []).find((item) => item.size) || (result.videos || [])[0];
      log(
        `解析成功 ${job.id}（${platformName}）：${(result.title || '').slice(0, 40)} ` +
          `清晰度=${best?.label || '-'} 体积=${best?.size ? (best.size / 1048576).toFixed(1) + 'MB' : '未知'} ` +
          `耗时=${((Date.now() - started) / 1000).toFixed(1)}s`
      );
    } else {
      log(`解析失败 ${job.id}（${platformName}）：${result.error}`);
    }
    await this.report(payload);
  }

  async loop() {
    log('解析器已启动，服务器：', this.config.server, '浏览器：', this.config.browser);
    this.startWatchdog();
    // 预热一次，让浏览器常驻，后续任务更快
    this.browser.start().catch((error) => log('浏览器预热失败:', error.message));

    while (this.running) {
      try {
        const response = await this.poll();
        if (response && response.job) {
          await this.handleJob(response.job);
          this.consecutiveFailures = 0;
        }
      } catch (error) {
        this.consecutiveFailures += 1;
        if (this.consecutiveFailures <= 3 || this.consecutiveFailures % 20 === 0) {
          log(`轮询失败（第 ${this.consecutiveFailures} 次）：${error.message}`);
        }
        const backoff = Math.min(this.config.retryDelayMs * this.consecutiveFailures, 60000);
        await new Promise((resolve) => setTimeout(resolve, backoff));
      }
    }
  }

  async shutdown() {
    this.running = false;
    await this.browser.stop().catch(() => {});
  }
}

(async () => {
  const config = loadConfig();

  // 兜底：任何未捕获异常都只记日志，不让进程死掉（进程一死群里就会报「解析器未连接」）。
  process.on('uncaughtException', (error) => {
    log('未捕获异常（已忽略，进程继续运行）：', error && error.stack ? error.stack.split('\n')[0] : String(error));
  });
  process.on('unhandledRejection', (reason) => {
    log('未处理的 Promise 拒绝（已忽略）：', reason instanceof Error ? reason.message : String(reason));
  });

  const agent = new Agent(config);
  process.on('SIGINT', async () => {
    log('收到退出信号，正在关闭…');
    await agent.shutdown();
    process.exit(0);
  });
  await agent.loop();
})().catch((error) => {
  log('启动失败：', error.message);
  process.exit(1);
});
