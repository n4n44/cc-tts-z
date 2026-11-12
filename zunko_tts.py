#!/usr/bin/env python3
"""Claude Code の transcript JSONL を監視して VOICEVOX (東北ずん子) で読み上げる。

使い方:
  zunko_tts.py --transcript PATH [--watch-pid PID]   # 監視モード
  zunko_tts.py --say "テキスト"                        # 動作確認
  zunko_tts.py --list-styles                          # ずん子のスタイル一覧

環境変数:
  VOICEVOX_URL   (default http://127.0.0.1:50021)
  ZUNKO_SPEAKER  (default 東北ずん子)
  ZUNKO_STYLE    (default ノーマル)
  ZUNKO_SPEED    (default 1.2)
  ZUNKO_TOOLS    1 ならツール呼び出しも短く通知する (default 0)
  ZUNKO_MAXCHARS 1メッセージで読む最大文字数。0 なら無制限 (default 0)
  ZUNKO_EXCLAIM  「！」で終わる文の語尾を持ち上げる量 (対数ピッチ, 0 で無効, default 0.2)
ミュート: ~/.cache/zunko-tts/mute が存在する間は読み上げない
Windows でも動く (再生は winsound)。リモートのエンジンを使うなら VOICEVOX_URL を設定する。
"""
import argparse, json, os, queue, re, subprocess, sys, tempfile, threading, time
import urllib.parse, urllib.request

URL = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021")
SPEAKER = os.environ.get("ZUNKO_SPEAKER", "東北ずん子")
STYLE = os.environ.get("ZUNKO_STYLE", "ノーマル")
SPEED = float(os.environ.get("ZUNKO_SPEED", "1.2"))
TOOLS = os.environ.get("ZUNKO_TOOLS", "0") == "1"
MAXCHARS = int(os.environ.get("ZUNKO_MAXCHARS", "0"))
EXCLAIM = float(os.environ.get("ZUNKO_EXCLAIM", "0.2"))
WIN = sys.platform == "win32"
CACHE = os.path.expanduser("~/.cache/zunko-tts")
MUTE = os.path.join(CACHE, "mute")
# 再生は子プロセスで同期再生する。winsound の SND_ASYNC は再生開始が遅れるうえ
# 次の PlaySound で前の音が切られるため、WAV 長で待つ方式だと文末が欠ける。
# 同期再生なら最後まで鳴り、flush 時は子プロセスを terminate すれば止まる。
if WIN:
    PLAY_CMD = [sys.executable, "-c",
                "import sys,winsound;winsound.PlaySound(sys.argv[1],winsound.SND_FILENAME)"]
    PLAY_KW = {"creationflags": 0x08000000}   # CREATE_NO_WINDOW
else:
    PLAY_CMD, PLAY_KW = ["paplay"], {}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


# ---------- VOICEVOX ----------
def http(method, path, params=None, body=None, timeout=30):
    url = URL + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def speakers():
    return json.loads(http("GET", "/speakers"))


def resolve_style_id():
    for sp in speakers():
        if sp["name"] == SPEAKER:
            for st in sp["styles"]:
                if st["name"] == STYLE:
                    return st["id"]
            names = [s["name"] for s in sp["styles"]]
            sys.exit(f"スタイル '{STYLE}' がありません: {names}")
    sys.exit(f"話者 '{SPEAKER}' がエンジンにいません")


def synth(text, sid):
    q = json.loads(http("POST", "/audio_query", {"text": text, "speaker": sid}))
    q["speedScale"] = SPEED
    if EXCLAIM and text.rstrip().endswith(("！", "!")):
        exclaim(q)
    return http("POST", "/synthesis", {"speaker": sid}, q, timeout=60)


def exclaim(q):
    """VOICEVOX は「！」を抑揚に反映しないので語尾を強調する。
    最後の音は元の高さのまま残し、その手前 4 音をなだらかに持ち上げる
    (「〜ましたよ！」なら「シマシタ」を上げて「ヨ」で落とす)。
    最後の音まで上げると不自然に聞こえた。"""
    if not q["accent_phrases"]:
        return
    voiced = [m for m in q["accent_phrases"][-1]["moras"] if m["pitch"] > 0]
    tail = voiced[:-1][-4:]
    for i, m in enumerate(tail, 1):
        m["pitch"] += EXCLAIM * i / len(tail)
    if tail:
        tail[-1]["vowel_length"] *= 1.15


# ---------- テキスト整形 ----------
def clean(text):
    text = re.sub(r"```.*?(```|$)", "。コードは省略。", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)          # [t](link)
    text = re.sub(r"https?://\S+", "URL", text)
    text = re.sub(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", "", text, flags=re.M)  # 表の区切り行
    text = re.sub(r"^\s{0,3}(#+|>|[-*+]|\d+\.)\s+", "", text, flags=re.M)
    text = re.sub(r"[*~#]+", "", text)
    text = re.sub(r"\|+", "、", text)            # 表のセル区切りは読点で区切る
    text = text.replace("_", " ")                # snake_case は英単語の区切りとして読む
    # VOICEVOX は半角スペースでポーズを入れるので、日本語に隣接するスペースは消す
    # (英単語どうしのスペースはポーズにならず、消すと読みが変わるので残す)
    text = re.sub(r"(?<=[^\x00-\x7f])[ \t　]+|[ \t　]+(?=[^\x00-\x7f])", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    if MAXCHARS and len(text) > MAXCHARS:
        text = text[:MAXCHARS] + "。以下省略。"
    return text


def sentences(text):
    for part in re.split(r"(?<=[。！？!?])|\n+", clean(text)):
        part = part.strip(" 　、,")
        if re.search(r"[\w぀-ヿ一-鿿]", part or ""):
            yield part


# ---------- パイプライン ----------
class Reader:
    def __init__(self, sid):
        self.sid = sid
        self.texts = queue.Queue()
        self.wavs = queue.Queue(maxsize=2)   # 再生中に次を先読み合成
        self.gen = 0                          # flush 世代
        self.player = None
        self.pending = 0                      # 未再生の文の数 (--say の終了待ち用)
        self.lock = threading.Lock()
        threading.Thread(target=self._synth_loop, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    def say(self, text):
        for s in sentences(text):
            with self.lock:
                self.pending += 1
            self.texts.put((self.gen, s))

    def _done(self):
        with self.lock:
            self.pending = max(0, self.pending - 1)

    def idle(self):
        return self.pending == 0

    def flush(self):
        with self.lock:
            self.gen += 1
            self.pending = 0
            p = self.player
            if p and p.poll() is None:
                p.terminate()
        for q in (self.texts, self.wavs):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass

    def _synth_loop(self):
        while True:
            g, s = self.texts.get()
            if g != self.gen or os.path.exists(MUTE):
                self._done()
                continue
            try:
                wav = synth(s, self.sid)
            except Exception as e:
                log("synth error:", e)
                self._done()
                time.sleep(1)
                continue
            if g == self.gen:
                self.wavs.put((g, wav))

    def _play_loop(self):
        while True:
            g, wav = self.wavs.get()
            if g != self.gen or os.path.exists(MUTE):
                self._done()
                continue
            fd, path = tempfile.mkstemp(suffix=".wav", dir=CACHE)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(wav)
                # 世代チェックと起動を flush と排他にして、古い文が鳴り出す隙をなくす
                with self.lock:
                    if g != self.gen:
                        continue
                    self.player = subprocess.Popen(PLAY_CMD + [path], **PLAY_KW)
                self.player.wait()
            except Exception as e:   # 再生プレイヤーの起動失敗でスレッドごと死なないように
                log("play error:", e)
                time.sleep(1)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                self._done()


def tool_phrase(block):
    name = block.get("name", "")
    inp = block.get("input", {}) or {}
    desc = inp.get("description")
    return f"{name}、{desc}" if desc else f"{name}を実行"


def handle(rec, reader, seen):
    t = rec.get("type")
    if rec.get("isSidechain"):
        return
    uid = rec.get("uuid")
    if uid in seen:
        return
    seen.add(uid)
    msg = rec.get("message") or {}
    content = msg.get("content")
    if t == "user":
        # 人間の新しいプロンプトが来たら読みかけを止める
        # (画像付きプロンプトは list になる。tool_result の list はプロンプトではない)
        if rec.get("isMeta"):
            return
        if isinstance(content, str) or (isinstance(content, list) and not any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content)):
            reader.flush()
        return
    if t != "assistant" or not isinstance(content, list):
        return
    for b in content:
        if b.get("type") == "text":
            reader.say(b.get("text", ""))
        elif b.get("type") == "tool_use" and TOOLS:
            reader.say(tool_phrase(b))


def alive(pid):
    if WIN:  # Windows の os.kill(pid, 0) はプロセスを終了させてしまうので使わない
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return k.GetLastError() == 5       # ACCESS_DENIED なら生きてる
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259               # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def watch(path, reader, watch_pid):
    seen = set()
    while not os.path.exists(path):
        time.sleep(0.2)
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)            # 既存の履歴は読まない
        buf = b""
        while True:
            chunk = f.read()
            if chunk:
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    try:
                        handle(json.loads(line), reader, seen)
                    except Exception as e:
                        log("parse error:", e)
            else:
                if watch_pid and not alive(watch_pid):
                    log("Claude Code が終了したので停止")
                    return
                time.sleep(0.15)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript")
    ap.add_argument("--watch-pid", type=int)
    ap.add_argument("--say")
    ap.add_argument("--list-styles", action="store_true")
    a = ap.parse_args()
    os.makedirs(CACHE, exist_ok=True)

    if a.list_styles:
        for sp in speakers():
            if sp["name"] == SPEAKER:
                for st in sp["styles"]:
                    print(st["id"], st["name"])
        return

    # エンジン起動待ち（最大60秒）
    for _ in range(120):
        try:
            http("GET", "/version", timeout=2)
            break
        except Exception:
            time.sleep(0.5)
    sid = resolve_style_id()
    reader = Reader(sid)

    if a.say:
        reader.say(a.say)
        while not reader.idle():
            time.sleep(0.1)
        return
    if not a.transcript:
        ap.error("--transcript か --say が必要です")
    log(f"watching {a.transcript} (style id {sid})")
    watch(a.transcript, reader, a.watch_pid)


if __name__ == "__main__":
    main()
