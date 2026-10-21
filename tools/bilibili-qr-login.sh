#!/bin/bash
# B 站扫码登录助手：在服务器上生成登录二维码 → 手机 B 站 App 扫码 → 自动把 Cookie 写进插件配置
#
# 用法（在 NekroAgent 宿主机上、与 docker 同一个 shell 里执行）：
#   sudo ./tools/bilibili-qr-login.sh [插件配置文件路径] [容器名]
#
# 默认：
#   配置文件 /home/ubuntu/srv/nekro_agent/plugin_data/local_video.video_forward/config.yaml
#   容器名   nekro_agent
#
# 为什么需要它：B 站 1080p+ 必须有登录态。手动从浏览器复制 SESSDATA 极易漏掉 bili_jct、
# 或被 YAML 折行破坏；扫码则由 B 站把一整套 Cookie 直接下发给我们，最可靠。
#
# 注意：二维码在容器内渲染，因此配置文件所在目录必须**挂载到容器内的同一路径**
#       （本项目默认就是这样：/home/ubuntu/srv/nekro_agent ↔ 容器内同路径）。

set -uo pipefail

CFG="${1:-/home/ubuntu/srv/nekro_agent/plugin_data/local_video.video_forward/config.yaml}"
CONTAINER="${2:-nekro_agent}"
WORKDIR="$(dirname "$CFG")/_bili_qr"
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36'
REF='https://www.bilibili.com/'
JAR=/tmp/bili_qr_jar.txt
HEAD=/tmp/bili_qr_headers.txt
POLL=/tmp/bili_qr_poll.json

log() { echo "[$(date +%H:%M:%S)] $*"; }

[ -f "$CFG" ] || { log "❌ 找不到配置文件：$CFG"; exit 1; }
mkdir -p "$WORKDIR"
chmod 777 "$WORKDIR" 2>/dev/null || true
rm -f "$JAR" "$HEAD" "$POLL"

render_qr() {
  docker exec -i "$CONTAINER" /app/.venv/bin/python - "$WORKDIR" <<'PY'
import pathlib, sys
import segno
base = pathlib.Path(sys.argv[1])
url = (base / 'url.txt').read_text().strip()
segno.make(url, error='m').save(str(base / 'qr.png'), scale=10, border=4)
print('   ✅ 二维码已生成：', (base / 'qr.png').stat().st_size, '字节')
PY
}

# ---------- 1. 申请二维码 ----------
log "1/4 申请 B 站登录二维码…"
curl -s --max-time 20 -A "$UA" -e "$REF" \
  'https://passport.bilibili.com/x/passport-login/web/qrcode/generate' > "$WORKDIR/qr.json"
python3 - "$WORKDIR" <<'PY'
import json, pathlib, sys
base = pathlib.Path(sys.argv[1])
d = json.loads((base / 'qr.json').read_text())
data = d.get('data') or {}
if not data.get('url'):
    print('   ❌ B 站没有返回二维码：', d)
    raise SystemExit(1)
(base / 'url.txt').write_text(data['url'])
(base / 'key.txt').write_text(data['qrcode_key'])
print('   qrcode_key =', data['qrcode_key'])
PY
[ -s "$WORKDIR/url.txt" ] || exit 1

# ---------- 2. 渲染二维码 ----------
log "2/4 渲染二维码 PNG…"
if ! render_qr; then
  log "   缺少 segno，尝试安装…"
  docker exec -i "$CONTAINER" /app/.venv/bin/python -m pip install --quiet segno
  render_qr || { log "❌ 二维码渲染失败"; exit 1; }
fi
log "   二维码文件：$WORKDIR/qr.png"
log "   （把它下载到本地手机能看到的屏幕上，或用 base64 传输后保存为 png 再扫）"

# ---------- 3. 轮询扫码 ----------
log "3/4 等待扫码（最长 170 秒）…"
KEY="$(cat "$WORKDIR/key.txt")"
OK=0
for i in $(seq 1 85); do
  sleep 2
  curl -s --max-time 10 -A "$UA" -e "$REF" -c "$JAR" -D "$HEAD" \
    "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key=$KEY" > "$POLL"
  CODE=$(python3 -c "
import json
try: print((json.load(open('$POLL')).get('data') or {}).get('code'))
except Exception: print('err')
" 2>/dev/null)
  case "$CODE" in
    0)     log "   ✅ 扫码并确认成功"; OK=1; break ;;
    86090) log "   已扫码，等手机点确认…" ;;
    86038) log "   ❌ 二维码失效，请重新运行本脚本"; break ;;
    *)     [ $((i % 15)) -eq 0 ] && log "   等待扫码…（$CODE）" ;;
  esac
done
[ "$OK" = 1 ] || { log "未完成登录"; exit 1; }

# ---------- 4. 写入配置 ----------
log "4/4 把 Cookie 写入插件配置…"
python3 - "$JAR" "$HEAD" "$CFG" <<'PY'
import json, pathlib, sys
jar, head, cfg_path = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
vals = {}
try:
    for raw in pathlib.Path(jar).read_text(errors='replace').splitlines():
        line = raw[len('#HttpOnly_'):] if raw.startswith('#HttpOnly_') else raw
        if not line or line.startswith('#'):
            continue
        p = line.split('\t')
        if len(p) >= 7 and p[5]:
            vals[p[5]] = p[6]
except FileNotFoundError:
    pass
try:
    for raw in pathlib.Path(head).read_text(errors='replace').splitlines():
        if raw.lower().startswith('set-cookie:'):
            pair = raw.split(':', 1)[1].split(';')[0].strip()
            if '=' in pair:
                k, v = pair.split('=', 1)
                vals.setdefault(k.strip(), v.strip())
except FileNotFoundError:
    pass
want = ['SESSDATA', 'bili_jct', 'DedeUserID', 'DedeUserID__ckMd5']
if 'SESSDATA' not in vals:
    print('   ❌ 没拿到 SESSDATA，实际字段：', ', '.join(sorted(vals)))
    raise SystemExit(1)
cookie = '; '.join(f'{k}={vals[k]}' for k in want if k in vals)

lines = cfg_path.read_text(encoding='utf-8').splitlines()
cfg_path.with_suffix(cfg_path.suffix + '.bak-bili-login').write_text(
    '\n'.join(lines) + '\n', encoding='utf-8'
)
out, skip, replaced = [], False, False
for line in lines:
    if line.startswith('bilibili_cookie'):
        out.append('bilibili_cookie: ' + json.dumps(cookie, ensure_ascii=False))
        replaced, skip = True, True
        continue
    if skip and (line.startswith(' ') or line.startswith('\t')):
        continue
    skip = False
    out.append(line)
if not replaced:
    out.append('bilibili_cookie: ' + json.dumps(cookie, ensure_ascii=False))
cfg_path.write_text('\n'.join(out) + '\n', encoding='utf-8')
print('   ✅ 已写入 bilibili_cookie（长度 %d），字段：%s'
      % (len(cookie), ', '.join(k for k in want if k in vals)))
PY

log "重启容器以加载配置…"
docker restart "$CONTAINER" >/dev/null && log "   已重启"
echo
log "完成。确认登录态："
echo "   curl -s http://127.0.0.1:8022/plugins/local_video.video_forward/ | grep -o '\"bilibili\":{[^}]*}'"
