"""
MultiTTSBOT - Discord 読み上げBOT
対応エンジン: VOICEROID2 / A.I.VOICE / VOICEVOX / CeVIO

起動前の準備
  pip install discord.py[voice] requests
  (VOICEROID2: pyvcroid2 / A.I.VOICE: aivoice_python / CeVIO: pywin32)
  ffmpeg に PATH を通しておくこと

設定ファイル（このスクリプトと同じフォルダ）
  token.txt    BOTトークンを1行だけ書く（環境変数 DISCORD_TOKEN があればそちらを優先）
  config.txt   その他の設定（エンジン・管理者ID・上限値・VOICEROID2の話者名など）

BOTを複数動かすとき:  python MultiTTSBOT.py config_bot2.txt
  （config側で token_file と data_dir をBOTごとに分けること）
"""
import asyncio
import configparser
import contextlib
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands

if not hasattr(discord.ui, "Label"):
    # 設定画面のポップアップ（中にプルダウンを置く）は discord.py 2.6 以上の機能
    raise SystemExit(
        f"discord.py が古いです（いま {discord.__version__}）。2.6 以上が必要です。\n"
        "次のコマンドで更新してください:  pip install -U discord.py"
    )

try:
    import requests
except ImportError:
    requests = None

log = logging.getLogger("multitts")

# ====================== 設定（config.txt を読み込む） ======================
BASE_DIR = Path(__file__).resolve().parent


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else BASE_DIR / p


# 起動時に別の設定ファイルを指定できる:  python MultiTTSBOT.py config_bot2.txt
CONFIG_PATH = _resolve(sys.argv[1] if len(sys.argv) > 1 else "config.txt")

_cfg = configparser.ConfigParser(interpolation=None)
_cfg.optionxform = str   # 大文字小文字をそのまま保つ
if CONFIG_PATH.exists():
    try:
        _cfg.read(CONFIG_PATH, encoding="utf-8-sig")   # メモ帳のBOM付きUTF-8でもOK
    except configparser.Error as _e:
        print(f"設定ファイル {CONFIG_PATH.name} の書き方に誤りがあります: {_e}")
        sys.exit(1)


def cfg_str(section: str, key: str, default: str = "") -> str:
    return _cfg.get(section, key, fallback=default).strip()


def cfg_int(section: str, key: str, default: int) -> int:
    try:
        return int(cfg_str(section, key, str(default)))
    except ValueError:
        return default


def cfg_list(section: str, key: str, default: str = "") -> list[str]:
    raw = cfg_str(section, key, default).replace("、", ",")
    return [x.strip() for x in raw.split(",") if x.strip()]


def cfg_bool(section: str, key: str, default: bool = False) -> bool:
    v = cfg_str(section, key).lower()
    if v in ("1", "true", "yes", "on", "オン", "はい"):
        return True
    if v in ("0", "false", "no", "off", "オフ", "いいえ"):
        return False
    return default


# --- [bot] ---
TOKEN_FILE = _resolve(cfg_str("bot", "token_file", "token.txt") or "token.txt")
DATA_DIR = _resolve(cfg_str("bot", "data_dir"))   # 空ならスクリプトのフォルダ
TEMP_DIR = DATA_DIR / "tmp"
DATA_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)
# ユーザー設定と辞書を置く場所。BOT同士で同じ場所を指定すると共有になる（空なら data_dir）
_shared = cfg_str("bot", "shared_dir")
SHARED_DIR = _resolve(_shared) if _shared else DATA_DIR
STORE_LOCK_TIMEOUT = 10   # 共有ファイルを、他のBOTが書き込み中のときに待つ最大秒数

# 入っていてよいサーバーのID。書いてあると、それ以外のサーバーからは自動で抜ける（空なら制限なし）
ALLOWED_GUILDS = {int(x) for x in cfg_list("bot", "allowed_guilds", "") if x.isdigit()}
# 許可リストにないサーバーから抜ける前に送るメッセージ（空なら何も送らずに抜ける）。\n で改行
LEAVE_MESSAGE = cfg_str(
    "bot", "leave_message",
    "このBOTは個人のプライベート向けに運用しているもので、こちらの設定ミスにより、"
    "誰でも招待できる状態になっていました。\\n"
    "申し訳ありませんが、このサーバーから退出します。ご迷惑をおかけしました。",
).replace("\\n", "\n")

OWNER_IDS = {
    int(x) for x in cfg_list("bot", "owner_ids", "")
    if x.isdigit()
}

ENGINE_ORDER = ("roid", "aivoice", "voicevox", "cevio")
ENGINE_LABELS = {
    "roid": "VOICEROID2",
    "aivoice": "A.I.VOICE",
    "voicevox": "VOICEVOX",
    "cevio": "CeVIO",
}
_enabled = set(cfg_list("bot", "engines")) or set(ENGINE_ORDER)
ENABLED_ENGINES = [k for k in ENGINE_ORDER if k in _enabled]
# BOTの再起動・クラッシュのあと、接続していたVCに自動で戻る
AUTO_RESUME = cfg_bool("bot", "auto_resume", True)
# 通信の不具合などでVCから切断されたとき、同じVCに自動で入り直す（/leave や無人で抜けたときは戻らない）
AUTO_REJOIN = cfg_bool("bot", "auto_rejoin", True)
REJOIN_DELAYS = (5, 15, 30)   # 入り直しを試すまでの待ち秒数（この回数まで試して、だめなら止める）
VOICE_RECOVER_WAIT_SECONDS = 45   # discord.py 自身の再接続（30秒で失敗する）の結果を待つ最大秒数

# --- [engine] ---
VOICEROID_INSTALL_PATH = cfg_str("engine", "voiceroid_install_path") or None
VOICEROID_INSTALL_PATH_X86 = cfg_str("engine", "voiceroid_install_path_x86") or None
AIVOICE_EDITOR_DIR = cfg_str("engine", "aivoice_editor_dir") or None
VOICEVOX_HOST = cfg_str("engine", "voicevox_host", "http://127.0.0.1:50021")
# 同じエンジンを複数のBOTで共有するとき、合成中は他のBOTを待たせる（VOICEROID2 / A.I.VOICE / CeVIO）
ENGINE_SHARED_LOCK = cfg_bool("engine", "shared_lock", True)
LOCK_TIMEOUT_SECONDS = cfg_int("engine", "lock_timeout_seconds", 120)
SYNTH_TIMEOUT_SECONDS = cfg_int("engine", "synth_timeout_seconds", 60)   # 合成を待つ最大秒数（0 で無制限）
RECONNECT_TIMEOUT_SECONDS = 180   # 再接続を待つ最大秒数（固まると、ほかのエンジンの再接続まで止まるので）
# BOT終了時にCeVIO本体も閉じるか。他のBOTも使っている可能性があるので、既定ではオフ
CEVIO_CLOSE_HOST = cfg_bool("engine", "cevio_close_host", False)
# 起動に失敗した・途中で落ちたエンジンへ、自動で再接続を試みる
ENGINE_AUTO_RECONNECT = cfg_bool("engine", "auto_reconnect", True)
RECONNECT_INTERVAL_SECONDS = max(10, cfg_int("engine", "reconnect_interval_seconds", 60))
# 読み上げが連続で失敗したとき、読み上げチャンネルに知らせる（回数 / 同じ通知を繰り返さない秒数）
FAIL_NOTICE_THRESHOLD = max(1, cfg_int("engine", "fail_notice_threshold", 3))
FAIL_NOTICE_COOLDOWN = max(0, cfg_int("engine", "fail_notice_cooldown_seconds", 600))

# --- [presence] ステータスの自動表示 ---
AUTO_PRESENCE_DEFAULT = cfg_bool("presence", "auto", True)
AUTO_PRESENCE_ACTIVE = cfg_str("presence", "auto_active", "{n}サーバーで読み上げ中")
AUTO_PRESENCE_IDLE = cfg_str("presence", "auto_idle", "/join で読み上げ開始")

# --- [log] ---
LOG_TO_FILE = cfg_bool("log", "file", True)
LOG_MAX_MB = max(1, cfg_int("log", "max_size_mb", 2))
LOG_BACKUPS = max(1, cfg_int("log", "backups", 5))
LOG_LEVEL = cfg_str("log", "level", "INFO").upper()
# 読み上げた発言を、どのサーバー・チャンネル・誰のものかログに残す（on/off）
LOG_READS = cfg_bool("log", "log_reads", True)
# 上に加えて、読み上げた文章そのものもログに残す（調べものをするときだけ on に。初期は off）
LOG_READ_TEXT = cfg_bool("log", "log_read_text", False)

# --- [limits] ---
VOICES_PER_PAGE = cfg_int("limits", "voices_per_page", 10)
MAX_QUEUE = cfg_int("limits", "max_queue", 20)                         # 1サーバーあたりの待ち行列の上限
MAX_TEXT_LEN = cfg_int("limits", "max_text_len", 300)                  # 1発言の最大読み上げ文字数
OVERFLOW_NOTICE_COOLDOWN = cfg_int("limits", "overflow_notice_seconds", 60)   # 読み飛ばしの通知を、くり返さない秒数
NAME_REPEAT_SECONDS = cfg_int("limits", "name_repeat_seconds", 60)     # 同じ人の連続発言で名前を省略する時間
DICT_MAX_ENTRIES = cfg_int("limits", "dict_max_entries", 500)
DICT_MAX_WORD = cfg_int("limits", "dict_max_word", 30)
DICT_MAX_READING = cfg_int("limits", "dict_max_reading", 60)

# --- [reading] ---
SKIP_PREFIXES = tuple(cfg_list("reading", "skip_prefixes", ";, ；, //, /"))   # この文字で始まる発言は読まない
# 未設定のサーバーでの初期値（サーバーごとに /settings で変更できる）
DEFAULT_READ_NAME = cfg_bool("reading", "default_read_name", False)
DEFAULT_ANNOUNCE = cfg_bool("reading", "default_announce", False)

# --- [voice] 話者を未設定のユーザーに自動で割り当てる候補エンジン ---
RANDOM_ENGINES = [k for k in cfg_list("voice", "random_engines", "aivoice, roid") if k in ENGINE_ORDER]

SETTINGS_FILE = "user_settings.json"
GUILD_SETTINGS_FILE = "guild_settings.json"
DICT_FILE = "dictionary.json"

SPEED_MIN, SPEED_MAX = 0.5, 2.0
PITCH_MIN, PITCH_MAX = 0.5, 2.0

# --- [voiceroid_names] VOICEROID2: 内部名 = 表示名（config.txt の記載が優先） ---
VOICEROID_DISPLAY_NAMES = {
    "ai_44": "月読アイ",
    "itako_emo_44": "東北イタコ",
    "kiritan_44": "東北きりたん",
    "seika_44": "京町セイカ",
    "sora_44": "桜乃そら",
    "tsuina_44": "ついなちゃん",
    "tsuina_west_44": "ついなちゃん（関西弁）",
    "zunko_44": "東北ずん子",
}
if _cfg.has_section("voiceroid_names"):
    VOICEROID_DISPLAY_NAMES.update(
        {k.strip(): v.strip() for k, v in _cfg.items("voiceroid_names") if k.strip() and v.strip()}
    )
# =================================================


@dataclass(frozen=True)
class Voice:
    key: str     # エンジン内で一意。保存・切替に使う（VOICEROID2なら内部名）
    label: str   # 画面に出す名前


class CrossProcessLock:
    """
    プロセスをまたいで効く名前付きMutex（Windows）。
    同じエンジンを別のBOT（別プロセス）と共有するとき、片方が合成中なら、もう片方は終わるまで待つ。
    取得と解放は同じスレッドで行うこと（エンジンの専用スレッド内で完結させている）。
    Windows以外、またはMutexを作れない場合は、何もしない（ロックなし）。
    """
    _WAIT_OK = (0x00000000, 0x00000080)   # 取得できた / 前の持ち主が異常終了したが取得できた

    def __init__(self, name: str):
        self._k32 = None
        self.handle = None
        if os.name != "nt":
            return
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.restype = ctypes.c_void_p
            k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            k32.WaitForSingleObject.restype = ctypes.c_uint32
            k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            k32.ReleaseMutex.argtypes = [ctypes.c_void_p]
            handle = k32.CreateMutexW(None, False, f"Local\\{name}")
            if handle:
                self._k32, self.handle = k32, handle
        except Exception as e:
            log.warning("共有ロックを作れませんでした（ロックなしで続行）: %s", e)

    @contextlib.contextmanager
    def hold(self, timeout_seconds: int):
        if not self.handle:
            yield
            return
        r = self._k32.WaitForSingleObject(self.handle, int(timeout_seconds * 1000))
        if r not in self._WAIT_OK:
            raise TimeoutError(f"他のBOTがエンジンを使用中で、{timeout_seconds}秒待っても空きませんでした")
        try:
            yield
        finally:
            self._k32.ReleaseMutex(self.handle)


# ====================== TTSエンジン ======================
class EngineHungError(RuntimeError):
    """エンジンが固まって、応答を待つのをあきらめた"""


class TTSEngine(ABC):
    """
    1エンジン = 専用の1スレッド。
    初期化も合成も話者切替も、すべてこのスレッドの中で順番に行う。
    （COMやDLLを複数スレッドから同時に触らないため）
    別のBOTと同じエンジンを共有する場合は、共有ロックで合成の順番も揃える。
    """
    name: str = "base"
    display_name: str = "Engine"
    shared_lock: bool = False   # True のエンジンは、他のBOTと同時に触らないようロックする
    can_reconnect: bool = True  # False のエンジンは、動作中の再初期化（再接続）をしない
    any_stuck: bool = False     # 固まったスレッドを1度でも残したか（普通の終了ができなくなる）

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tts-{self.name}")
        self.voices: list[Voice] = []
        self._by_key: dict[str, Voice] = {}
        self.fail_streak = 0                    # 連続で読み上げに失敗した回数
        self.last_error: Optional[str] = None   # 直近のエラー内容（/status で表示）
        self._hung = asyncio.Event()            # 専用スレッドが固まっている間だけセットされる
        self._xlock = CrossProcessLock(f"MultiTTSBOT_{self.name}") if (self.shared_lock and ENGINE_SHARED_LOCK) else None

    async def _call(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, fn, *args)

    def _run_locked(self, fn, *args):
        """共有ロックを取ってから fn を実行する（ロックなしの設定なら、そのまま実行）"""
        if self._xlock is None:
            return fn(*args)
        with self._xlock.hold(LOCK_TIMEOUT_SECONDS):
            return fn(*args)

    @property
    def hung(self) -> bool:
        return self._hung.is_set()

    def _mark_hung(self, fut, what: str):
        """
        固まった処理を見放す。スレッドは外から止められないので、処理が戻るまで新しい仕事は受けない。
        戻ってきたら自動で復帰する（エンジンを再起動すると、固まっていた処理はエラーで戻る）。
        """
        self._hung.set()
        TTSEngine.any_stuck = True
        log.error("[%s] %sが応答しません。戻るまでこのエンジンは使えません", self.display_name, what)

        def on_return(f):
            self._hung.clear()
            log.info("[%s] 固まっていた処理が戻りました", self.display_name)
            if not f.cancelled() and f.exception() is None and isinstance(f.result(), str):
                _remove_quietly(f.result())   # もう誰も待っていない音声は捨てる

        fut.add_done_callback(on_return)

    def _hung_error(self) -> EngineHungError:
        return EngineHungError(f"{self.display_name} が固まっているため、読み上げられません")

    async def start(self, timeout: Optional[int] = None):
        fut = asyncio.get_running_loop().run_in_executor(self.executor, self._run_locked, self._setup)
        try:
            await asyncio.wait({fut}, timeout=timeout)
        except asyncio.CancelledError:
            fut.cancel()
            raise
        if not fut.done():
            self._mark_hung(fut, "接続")
            raise self._hung_error()
        fut.result()
        self._by_key = {v.key: v for v in self.voices}
        self.fail_streak = 0
        self.last_error = None

    async def reconnect(self):
        """エンジンを初期化し直す（落ちていたエンジンが復活したときの再接続用）"""
        if self.hung:
            raise self._hung_error()   # 専用スレッドが空くまで、再接続もできない
        await self.start(RECONNECT_TIMEOUT_SECONDS)

    async def synth(self, text: str, voice_key: str, speed: float, pitch: float) -> Optional[str]:
        """話者切替→合成を1つのまとまりとして専用スレッドで実行し、wavのパスを返す"""
        try:
            path = await self._synth_guarded(text, voice_key, speed, pitch)
        except Exception as e:
            self.fail_streak += 1
            if isinstance(e, EngineHungError):
                self.fail_streak = max(self.fail_streak, FAIL_NOTICE_THRESHOLD)   # 1回でも、すぐ知らせて再接続を試す
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            raise
        self.fail_streak = 0   # 成功したら数え直し（読み飛ばしの None も、エンジンは正常）
        return path

    async def _synth_guarded(self, *args) -> Optional[str]:
        """
        固まったら SYNTH_TIMEOUT_SECONDS であきらめる。
        数えるのは合成が始まってからで、順番待ちや、他のBOTを待つ時間は含まない。
        """
        if self.hung:
            raise self._hung_error()
        if SYNTH_TIMEOUT_SECONDS <= 0:
            return await self._call(self._run_locked, self._synth, *args)

        loop = asyncio.get_running_loop()
        begun = asyncio.Event()

        def job():
            loop.call_soon_threadsafe(begun.set)
            return self._synth(*args)

        fut = loop.run_in_executor(self.executor, self._run_locked, job)
        waits = {asyncio.ensure_future(begun.wait()), asyncio.ensure_future(self._hung.wait())}
        try:
            await asyncio.wait({fut, *waits}, return_when=asyncio.FIRST_COMPLETED)
            if not fut.done() and begun.is_set():
                await asyncio.wait({fut}, timeout=SYNTH_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            fut.cancel()
            raise
        finally:
            for w in waits:
                w.cancel()
        if fut.done():
            return fut.result()
        if begun.is_set():
            self._mark_hung(fut, f"合成（{SYNTH_TIMEOUT_SECONDS}秒）")
        else:
            fut.cancel()   # 順番待ちの間に、前の処理が固まった
        raise self._hung_error()

    async def stop(self):
        if not self.hung:
            fut = asyncio.get_running_loop().run_in_executor(self.executor, self._teardown)
            with contextlib.suppress(Exception):
                await asyncio.wait({fut}, timeout=10)
                if not fut.done():
                    self._mark_hung(fut, "終了処理")
        self.executor.shutdown(wait=False, cancel_futures=True)

    def find_voice(self, key) -> Optional[Voice]:
        return self._by_key.get(key) if key is not None else None

    def legacy_keys(self) -> list[Optional[str]]:
        """旧形式の設定（エンジン内の番号 voice_index）を、話者キーに直すための一覧。
        旧バージョンが「番号」として数えていた並びと同じ順で返す（通常は今の一覧と同じ）。
        対応づけられない番号は None"""
        return [v.key for v in self.voices]

    @staticmethod
    def _new_wav() -> str:
        fd, path = tempfile.mkstemp(suffix=".wav", dir=TEMP_DIR)
        os.close(fd)
        return path

    @abstractmethod
    def _setup(self):
        ...

    @abstractmethod
    def _synth(self, text: str, voice_key: str, speed: float, pitch: float) -> str:
        ...

    def _teardown(self):
        pass


# ---------- VOICEROID2 ----------
class VoiceroidEngine(TTSEngine):
    name = "roid"
    display_name = "VOICEROID2"
    shared_lock = True
    can_reconnect = False   # DLLを同じプロセスで初期化し直すと不安定になるので、動作中の再接続はしない

    def _setup(self):
        import pyvcroid2
        if VOICEROID_INSTALL_PATH and VOICEROID_INSTALL_PATH_X86:
            self.vc = pyvcroid2.VcRoid2(
                install_path=VOICEROID_INSTALL_PATH,
                install_path_x86=VOICEROID_INSTALL_PATH_X86,
            )
        else:
            self.vc = pyvcroid2.VcRoid2()

        lang_list = self.vc.listLanguages()
        if "standard" in lang_list:
            self.vc.loadLanguage("standard")
        elif lang_list:
            self.vc.loadLanguage(lang_list[0])
        else:
            raise RuntimeError("言語ライブラリなし")

        raw = list(self.vc.listVoices())
        if not raw:
            raise RuntimeError("ボイスなし")
        # 内部名は key、日本語名は label。対応表にないボイスは内部名のまま表示する
        self.voices = [Voice(v, VOICEROID_DISPLAY_NAMES.get(v, v)) for v in raw]

        self._loaded = raw[0]
        self.vc.loadVoice(raw[0])
        self.vc.param.volume = 1.0
        self.vc.param.emphasis = 1.0
        self.vc.param.pauseMiddle = 80
        self.vc.param.pauseLong = 120
        self.vc.param.pauseSentence = 150
        self.vc.param.masterVolume = 1.0
        log.info("[VOICEROID2] OK / %d話者", len(raw))

    def _ensure_voice(self, key: str):
        if key == self._loaded:
            return
        dll = getattr(self.vc, "_VcRoid2__dll", None) or getattr(self.vc, "__dll", None)
        if dll is not None:
            try:
                dll.AITalkAPI_VoiceClear()
            except Exception as e:
                log.warning("[VOICEROID2] VoiceClear: %s", e)
        try:
            self.vc.loadVoice(key)
        except Exception as e:
            if "ALREADY_LOADED" not in str(e):
                raise
        self._loaded = key
        log.info("[VOICEROID2] → %s", key)

    @staticmethod
    def _to_shiftjis_safe(text: str) -> str:
        """
        pyvcroid2 は文字を Shift_JIS に変換して読み上げるため、変換できない文字があると例外になる。
        ・「～」(U+FF5E) など、Windows版とPython版の対応表が違う文字は、使える文字に置き換える
        ・それでも変換できない文字（♡ や珍しい漢字など）は取り除く
        """
        out = []
        for ch in text:
            try:
                ch.encode("shift_jis")
                out.append(ch)
                continue
            except UnicodeEncodeError:
                pass
            try:
                # cp932 で表せる文字は、同じバイト列を shift_jis として読み直して対応する文字にする
                out.append(ch.encode("cp932").decode("shift_jis"))
                continue
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            # 異体字や丸数字などは、似た文字に置き換える（例: 髙→高、①→1、㈱→(株)）
            alt = {"髙": "高", "﨑": "崎", "德": "徳", "𠮷": "吉"}.get(ch) or unicodedata.normalize("NFKC", ch)
            try:
                alt.encode("shift_jis")
                out.append(alt)
            except UnicodeEncodeError:
                pass   # それでも読めない文字は捨てる
        return "".join(out)

    def _synth(self, text, voice_key, speed, pitch) -> Optional[str]:
        text = self._to_shiftjis_safe(text)
        if not text.strip():
            return None   # 読める文字が1つも残らなかった（呼び出し側で読み飛ばす）
        self._ensure_voice(voice_key)
        self.vc.param.speed = float(speed)
        self.vc.param.pitch = float(pitch)
        speech, _ = self.vc.textToSpeech(text)
        path = self._new_wav()
        with open(path, "wb") as f:
            f.write(speech)
        return path


# ---------- A.I.VOICE ----------
class AIVoiceEngine(TTSEngine):
    name = "aivoice"
    display_name = "A.I.VOICE"
    shared_lock = True

    def _setup(self):
        from aivoice_python import AIVoiceTTsControl, HostStatus

        if AIVOICE_EDITOR_DIR:
            self.tts = AIVoiceTTsControl(editor_dir=AIVOICE_EDITOR_DIR)
        else:
            self.tts = AIVoiceTTsControl()

        hosts = self.tts.get_available_host_names()
        if not hosts:
            raise RuntimeError("Editorなし")
        self.tts.initialize(hosts[0])
        if self.tts.status == HostStatus.NotRunning:
            log.info("[A.I.VOICE] Editor 起動中...")
            self.tts.start_host()
        self.tts.connect()

        # 切替に使うのはプリセット名なので、一覧もプリセット名に統一する
        names = list(self.tts.voice_preset_names) or list(self.tts.voice_names)
        if not names:
            raise RuntimeError("ボイスなし")
        self.voices = [Voice(n, n) for n in names]

        # 旧バージョンは「voice_names（なければ preset_names）」の並びで番号を数えていた
        self._legacy_names = list(self.tts.voice_names) or list(self.tts.voice_preset_names)

        self._current = None
        try:
            cur = self.tts.current_voice_preset_name
        except Exception:
            cur = None
        if cur in names:
            self._current = cur
        else:
            self.tts.current_voice_preset_name = names[0]
            self._current = names[0]
        log.info("[A.I.VOICE] OK / %d話者", len(names))

    def legacy_keys(self) -> list[Optional[str]]:
        known = {v.key for v in self.voices}
        legacy = getattr(self, "_legacy_names", None) or [v.key for v in self.voices]
        out: list[Optional[str]] = []
        for i, n in enumerate(legacy):
            if n in known:
                out.append(n)
            elif len(legacy) == len(self.voices):
                out.append(self.voices[i].key)   # 名前の種類が違うだけで、並びは同じとみなす
            else:
                out.append(None)
        return out

    def _ensure_connected(self):
        from aivoice_python import HostStatus
        try:
            if getattr(self.tts, "status", None) == HostStatus.NotRunning:
                log.info("[A.I.VOICE] Editor 未起動 → start_host")
                self.tts.start_host()
            if hasattr(self.tts, "connect"):
                self.tts.connect()
        except Exception as e:
            log.warning("[A.I.VOICE] 再接続試行: %s", e)
            try:
                self.tts.start_host()
                if hasattr(self.tts, "connect"):
                    self.tts.connect()
            except Exception as e2:
                raise RuntimeError(f"A.I.VOICE 再接続失敗: {e2}") from e2

    def _set_master_control(self, speed: float, pitch: float, volume: float = 1.0):
        mc = {
            "Volume": float(volume),
            "Speed": float(speed),
            "Pitch": float(pitch),
            "PitchRange": 1.0,
            "MiddlePause": 150,
            "LongPause": 370,
            "SentencePause": 800,
        }
        js = json.dumps(mc)
        for attr in ("master_control", "MasterControl"):
            if hasattr(self.tts, attr):
                setattr(self.tts, attr, js)
                return
        inner = getattr(self.tts, "_tts_control", None) or getattr(self.tts, "tts_control", None)
        if inner is not None:
            for attr in ("MasterControl", "master_control"):
                if hasattr(inner, attr):
                    setattr(inner, attr, js)
                    return

    def _synth(self, text, voice_key, speed, pitch) -> str:
        self._ensure_connected()
        # Editorは他のBOTも操作する可能性があるので、覚えている値ではなく、いまの実際の話者と比べる
        try:
            now = self.tts.current_voice_preset_name
        except Exception:
            now = None
        if voice_key != now:
            self.tts.current_voice_preset_name = voice_key
            log.info("[A.I.VOICE] → %s", voice_key)
        self._current = voice_key
        self._set_master_control(speed, pitch)
        self.tts.text = text
        path = self._new_wav()
        self.tts.save_audio_to_file(path)
        return path

    def _teardown(self):
        with contextlib.suppress(Exception):
            self.tts.disconnect()


# ---------- VOICEVOX ----------
class VoicevoxEngine(TTSEngine):
    name = "voicevox"
    display_name = "VOICEVOX"

    def __init__(self, host: str = VOICEVOX_HOST):
        super().__init__()
        self.host = host.rstrip("/")

    def _setup(self):
        if requests is None:
            raise RuntimeError("pip install requests")
        old = getattr(self, "session", None)   # 再接続のとき、前のセッションを閉じる
        if old is not None:
            with contextlib.suppress(Exception):
                old.close()
        self.session = requests.Session()
        r = self.session.get(f"{self.host}/speakers", timeout=10)
        r.raise_for_status()
        voices = []
        for sp in r.json():
            name = sp.get("name", "?")
            for style in sp.get("styles", []):
                sn = style.get("name", "")
                label = f"{name}（{sn}）" if sn else name
                voices.append(Voice(str(int(style["id"])), label))   # key = スタイルID
        if not voices:
            raise RuntimeError("話者なし（ENGINE起動確認）")
        self.voices = voices
        log.info("[VOICEVOX] OK / %d話者", len(voices))

    def _synth(self, text, voice_key, speed, pitch) -> str:
        sid = int(voice_key)
        q = self.session.post(
            f"{self.host}/audio_query",
            params={"text": text, "speaker": sid},
            timeout=30,
        )
        q.raise_for_status()
        query = q.json()
        query["speedScale"] = float(speed)
        # VOICEVOXのpitchScaleは±0.15程度が実用範囲。1.0を中心に縮めて割り当てる
        query["pitchScale"] = (float(pitch) - 1.0) * 0.15
        s = self.session.post(
            f"{self.host}/synthesis",
            params={"speaker": sid},
            json=query,
            timeout=60,
        )
        s.raise_for_status()
        path = self._new_wav()
        with open(path, "wb") as f:
            f.write(s.content)
        return path

    def _teardown(self):
        with contextlib.suppress(Exception):
            self.session.close()


# ---------- CeVIO AI ----------
class CeVIOEngine(TTSEngine):
    name = "cevio"
    display_name = "CeVIO"
    shared_lock = True

    def _setup(self):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()   # このスレッド専用にCOMを初期化（以後もこのスレッドだけで使う）
        self.service = win32com.client.Dispatch("CeVIO.Talk.RemoteService2.ServiceControl2V40")
        try:
            self.service.StartHost(False)
        except Exception:
            pass
        self.talker = win32com.client.Dispatch("CeVIO.Talk.RemoteService2.Talker2V40")
        arr = self.talker.AvailableCasts
        casts = [arr.At(i) for i in range(arr.Length)]
        if not casts:
            raise RuntimeError("キャストなし")
        self.voices = [Voice(c, c) for c in casts]
        self.talker.Cast = casts[0]
        log.info("[CeVIO] OK / %d話者", len(casts))

    @staticmethod
    def _to_cevio(v: float) -> int:
        # 1.0 → 50（CeVIOの標準）、0.5 → 0、2.0 → 100 になるよう区間ごとに換算
        v = max(0.5, min(2.0, float(v)))
        x = (v - 1.0) / 0.5 * 50 if v <= 1.0 else (v - 1.0) / 1.0 * 50
        return max(0, min(100, int(round(50 + x))))

    def _synth(self, text, voice_key, speed, pitch) -> str:
        self.talker.Cast = voice_key
        self.talker.Speed = self._to_cevio(speed)
        self.talker.Tone = self._to_cevio(pitch)
        self.talker.Volume = 100
        path = self._new_wav()
        if not self.talker.OutputWaveToFile(text, path):
            raise RuntimeError("CeVIO OutputWaveToFile 失敗")
        return path

    def _teardown(self):
        if CEVIO_CLOSE_HOST:   # 他のBOTも使っている可能性があるので、既定では閉じない
            with contextlib.suppress(Exception):
                self.service.CloseHost(0)
        with contextlib.suppress(Exception):
            import pythoncom
            pythoncom.CoUninitialize()


ENGINE_CLASSES = {
    "roid": VoiceroidEngine,
    "aivoice": AIVoiceEngine,
    "voicevox": VoicevoxEngine,
    "cevio": CeVIOEngine,
}


# ====================== 設定・辞書 ======================
class JsonStore:
    """壊れたら退避して続行 / 保存は一時ファイル経由で上書き（書き込み中の電源断に強い）"""

    def __init__(self, filename: str, directory: Optional[Path] = None):
        self.path = (directory or DATA_DIR) / filename
        self.data: dict = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("JSONの最上位がオブジェクトではありません")
            self.data = data
        except Exception as e:
            backup = self.path.with_name(self.path.name + ".corrupt")
            log.error("%s の読み込みに失敗: %s → %s に退避します", self.path.name, e, backup.name)
            with contextlib.suppress(OSError):
                os.replace(self.path, backup)
            self.data = {}

    def _write(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        for attempt in range(5):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                # Windowsでは、ウイルス対策ソフトなどが一瞬ファイルを開いていると置き換えに失敗する
                if attempt == 4:
                    raise
                time.sleep(0.05)

    def save(self):
        self._write()


class SharedStore(JsonStore):
    """
    複数のBOTで共有できる保存ファイル（ユーザー設定・辞書）。
    - 他のBOTがファイルを書き換えたら、次に読むときに読み直す
    - 書き込みは、BOT同士でロックを取ってから「最新を読み直す → 変更を加える → 保存」の順で行う
      （どちらのBOTの変更も消えない）
    BOTが1つだけでも、そのまま使える。
    """

    def __init__(self, filename: str, directory: Optional[Path] = None):
        self.path = (directory or DATA_DIR) / filename
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict = {}
        self._stamp = None
        name = hashlib.md5(str(self.path.resolve()).lower().encode("utf-8")).hexdigest()[:16]
        self._lock = CrossProcessLock(f"MultiTTSBOT_store_{name}")
        with self._hold():
            self._load()

    def _hold(self):
        return self._lock.hold(STORE_LOCK_TIMEOUT)

    def _file_stamp(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        # 保存のたびにファイルを置き換えるので、st_ino（ファイルの番号）も変わる。時刻が同じでも見分けられる
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _load(self):
        super()._load()
        self._stamp = self._file_stamp()
        self._on_reload()

    def _on_reload(self):
        pass

    def refresh(self):
        """他のBOTが書き換えていたら読み直す"""
        if self._file_stamp() != self._stamp:
            with self._hold():
                self._load()

    def mutate(self, fn):
        """ロックを取り、最新を読み直してから fn(data) で変更し、保存する。fn の戻り値を返す"""
        with self._hold():
            if self._file_stamp() != self._stamp:
                self._load()
            result = fn(self.data)
            self._write()
            self._stamp = self._file_stamp()
        return result

    def save(self):
        self.mutate(lambda d: None)


class UserSettings(SharedStore):
    # auto: 未設定だったので自動で割り当てた話者（/settings で選ぶと False になる）
    DEFAULTS = {"engine": None, "voice": None, "auto": False, "speed": 1.0, "pitch": 1.0}

    def get(self, user_id: int) -> dict:
        self.refresh()
        s = self.data.setdefault(str(user_id), {})
        for k, v in self.DEFAULTS.items():
            s.setdefault(k, v)
        return s

    def update(self, user_id: int, **kwargs):
        def fn(d):
            s = d.setdefault(str(user_id), {})
            s.update(kwargs)
            if "voice" in kwargs:   # 旧形式（番号保存）の値は捨てる
                s.pop("voice_index", None)
                s.pop("unified_index", None)
        self.mutate(fn)

    def assign_auto(self, user_id: int, seen: tuple, engine: str, voice: str) -> tuple:
        """
        自動割り当ての話者を保存する。ただし、見たときから他のBOTが先に決めていたら、そちらを残す
        （2つのBOTで同時に初発言しても、声が食い違わないように）。保存されている (engine, voice) を返す
        """
        def fn(d):
            s = d.setdefault(str(user_id), {})
            if (s.get("engine"), s.get("voice")) == seen:
                s.update(engine=engine, voice=voice, auto=True)
                s.pop("voice_index", None)
                s.pop("unified_index", None)
            return s.get("engine"), s.get("voice")
        return self.mutate(fn)


class GuildSettings(JsonStore):
    DEFAULTS = {"read_name": DEFAULT_READ_NAME, "announce": DEFAULT_ANNOUNCE, "volume": 100}

    def get(self, guild_id: int) -> dict:
        s = self.data.setdefault(str(guild_id), {})
        for k, v in self.DEFAULTS.items():
            s.setdefault(k, v)
        return s

    def update(self, guild_id: int, **kwargs):
        self.get(guild_id).update(kwargs)
        self.save()


_ASCII_WORD = "A-Za-z0-9_"


def validate_dict_word(word: str) -> Optional[str]:
    """辞書に登録できない語なら、理由を返す（登録できるなら None）。
    ひらがな・カタカナ1文字や記号だけの語は、あちこちの文に誤って当たるので登録できない"""
    if not word.strip():
        return "表記が空です"
    if len(word) == 1 and ("ぁ" <= word <= "ヿ" or "ｦ" <= word <= "ﾟ"):
        return "ひらがな・カタカナ1文字は登録できません（ほかの言葉の中にも当たってしまうため）"
    if not any(ch.isalnum() for ch in word):
        return "記号だけの語は登録できません"
    return None


class Dictionary(SharedStore):
    def __init__(self, filename: str, directory: Optional[Path] = None):
        self._cache: dict[str, tuple] = {}
        super().__init__(filename, directory)

    def _on_reload(self):
        self._cache.clear()   # 他のBOTが辞書を変えたら、置換ルールを作り直す

    def get_map(self, guild_id: int) -> dict[str, str]:
        self.refresh()
        return self.data.get(str(guild_id), {})

    def add(self, guild_id: int, word: str, reading: str):
        def fn(d):
            d.setdefault(str(guild_id), {})[word] = reading
        self.mutate(fn)
        self._cache.pop(str(guild_id), None)

    def replace(self, guild_id: int, old_word: str, word: str, reading: str):
        """登録済みの語を書き換える（表記を変えた場合は古い語を消す）"""
        def fn(d):
            m = d.setdefault(str(guild_id), {})
            if old_word != word:
                m.pop(old_word, None)
            m[word] = reading
        self.mutate(fn)
        self._cache.pop(str(guild_id), None)

    def remove(self, guild_id: int, word: str) -> bool:
        def fn(d):
            m = d.get(str(guild_id), {})
            if word in m:
                del m[word]
                return True
            return False
        removed = self.mutate(fn)
        self._cache.pop(str(guild_id), None)
        return removed

    def _compiled(self, guild_id: int):
        self.refresh()
        key = str(guild_id)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        m = self.get_map(guild_id)
        # 安全でない語（昔に登録された1文字など）は使わない
        words = [w for w in m if validate_dict_word(w) is None]
        if not words:
            result = (None, {})
        else:
            lookup = {w.casefold(): m[w] for w in words}
            parts = []
            for w in sorted(words, key=len, reverse=True):   # 長い語を優先
                p = re.escape(w)
                if re.match(f"[{_ASCII_WORD}]", w[0]):    # 英数字で始まる語は、英数字の途中には当てない
                    p = f"(?<![{_ASCII_WORD}])" + p
                if re.match(f"[{_ASCII_WORD}]", w[-1]):
                    p = p + f"(?![{_ASCII_WORD}])"
                parts.append(p)
            result = (re.compile("|".join(parts), re.IGNORECASE), lookup)
        self._cache[key] = result
        return result

    def apply(self, guild_id: int, text: str) -> str:
        pattern, lookup = self._compiled(guild_id)
        if pattern is None:
            return text
        # 1回で置換するので、置換結果が別の語に再マッチしない
        return pattern.sub(lambda mo: lookup.get(mo.group(0).casefold(), mo.group(0)), text)


# ====================== グローバル状態 ======================
engines: dict[str, TTSEngine] = {}
players: dict[int, "GuildPlayer"] = {}
settings = UserSettings(SETTINGS_FILE, SHARED_DIR)       # ユーザー設定（shared_dir を指定するとBOT同士で共有）
guild_settings = GuildSettings(GUILD_SETTINGS_FILE)      # サーバー設定（BOTごと）
dictionary = Dictionary(DICT_FILE, SHARED_DIR)           # 辞書（shared_dir を指定するとBOT同士で共有）
presence_store = JsonStore("presence.json")   # BOTのステータス・プレイ中表示（再起動後も保持）
sessions_store = JsonStore("sessions.json")   # 接続中のVC（再起動後に自動で戻るため）
RESTART_REQUESTED = False
_bg_tasks: set = set()


def spawn(coro) -> asyncio.Task:
    """待たずに走らせるタスク。参照を保持して、途中で回収されないようにする"""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task

STATUS_MAP = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.do_not_disturb,
    "invisible": discord.Status.invisible,
}
STATUS_LABELS = {
    "online": "オンライン",
    "idle": "退席中",
    "dnd": "取り込み中",
    "invisible": "オフライン表示",
}
ACTIVITY_TYPES = {
    "playing": discord.ActivityType.playing,
    "listening": discord.ActivityType.listening,
    "watching": discord.ActivityType.watching,
    "competing": discord.ActivityType.competing,
}
ACTIVITY_LABELS = {
    "playing": "プレイ中",
    "listening": "再生中",
    "watching": "視聴中",
    "competing": "参戦中",
}
ACTIVITY_MAX_LEN = 128


def build_activity(kind: str, text: Optional[str]) -> Optional[discord.Activity]:
    if not text:
        return None
    return discord.Activity(type=ACTIVITY_TYPES.get(kind, ACTIVITY_TYPES["playing"]), name=text)


def stored_presence() -> tuple[str, str, Optional[str]]:
    """保存済みの (status, kind, text)。壊れた値は既定に戻す"""
    d = presence_store.data
    status = d.get("status") if d.get("status") in STATUS_MAP else "online"
    kind = d.get("kind") if d.get("kind") in ACTIVITY_TYPES else "playing"
    text = d.get("text") if isinstance(d.get("text"), str) and d.get("text") else None
    return status, kind, text


def presence_auto() -> bool:
    """ステータスを自動表示（読み上げ中のサーバー数）にするか。
    設定画面で一度も指定していない場合は config の初期値。ただし以前に自分で文字を設定していたなら、それを消さない"""
    d = presence_store.data
    if isinstance(d.get("auto"), bool):
        return d["auto"]
    return AUTO_PRESENCE_DEFAULT and not d.get("text")


def effective_presence() -> tuple[str, str, Optional[str]]:
    """いま実際に表示する (status, kind, text)"""
    status, kind, text = stored_presence()
    if presence_auto():
        n = sum(1 for p in players.values() if p.active)
        template = AUTO_PRESENCE_ACTIVE if n else AUTO_PRESENCE_IDLE
        text = template.replace("{n}", str(n))[:ACTIVITY_MAX_LEN] if template else None
    return status, kind, text


def request_presence_refresh():
    """接続数が変わったときに呼ぶ。短時間の連続変更をまとめて反映する（レート制限対策）"""
    ev = getattr(bot, "_presence_event", None)
    if ev is not None:
        ev.set()


@dataclass
class UVoice:
    engine: TTSEngine
    voice: Voice

    @property
    def label(self) -> str:
        return f"[{self.engine.display_name}] {self.voice.label}"


unified: list[UVoice] = []
unified_index_by_key: dict[tuple[str, str], int] = {}


def rebuild_unified_voices():
    unified.clear()
    unified_index_by_key.clear()
    for key in ENGINE_ORDER:
        eng = engines.get(key)
        if eng is None:
            continue
        for v in eng.voices:
            unified_index_by_key[(eng.name, v.key)] = len(unified)
            unified.append(UVoice(eng, v))
    log.info("統合話者数: %d", len(unified))


def default_choice() -> tuple[TTSEngine, Voice]:
    for key in ENGINE_ORDER:
        eng = engines.get(key)
        if eng and eng.voices:
            return eng, eng.voices[0]
    raise RuntimeError("使用可能なエンジンがありません")


def random_pool() -> list[tuple[TTSEngine, Voice]]:
    """未設定ユーザーへの自動割り当て候補（config の random_engines に載っている稼働中エンジンの全話者）"""
    pool = []
    for key in RANDOM_ENGINES:
        eng = engines.get(key)
        if eng:
            pool.extend((eng, v) for v in eng.voices)
    return pool


def auto_assign(user_id: int, persist: bool) -> tuple[TTSEngine, Voice]:
    """
    候補からランダムに話者を決める。
    persist=True : 決めた話者を保存する（以後ずっと同じ声。毎回変わらないように）
    persist=False: 保存せず、ユーザーIDから決まる固定の話者を返す（エンジン一時停止中の代役）
    """
    pool = random_pool()
    if not pool:
        return default_choice()
    if persist:
        eng, v = random.choice(pool)
        s = settings.get(user_id)
        seen = (s.get("engine"), s.get("voice"))
        try:
            got_eng, got_voice = settings.assign_auto(user_id, seen, eng.name, v.key)
        except (OSError, TimeoutError):
            log.exception("自動割り当ての保存に失敗（今回だけこの声で読みます）")
            return eng, v
        if (got_eng, got_voice) != (eng.name, v.key):
            # 他のBOTが先に決めていた → そちらの声を使う（このBOTで使えなければ今回だけ代役）
            other = engines.get(got_eng)
            ov = other.find_voice(got_voice) if other else None
            return (other, ov) if ov is not None else random.Random(user_id).choice(pool)
        log.info("話者を自動割り当て: user=%s → [%s] %s", user_id, eng.display_name, v.label)
        return eng, v
    return random.Random(user_id).choice(pool)


def _legacy_voice(eng: TTSEngine, idx) -> Optional[Voice]:
    """旧形式の番号 → 話者。対応づけられなければ None"""
    keys = eng.legacy_keys()
    if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(keys) and keys[idx] is not None:
        return eng.find_voice(keys[idx])
    return None


_migration_warned: set = set()


def migrate_legacy_settings() -> int:
    """
    旧形式（エンジン内の番号 voice_index で保存）の設定を、話者キーの形式に1回だけ書き換える。
    今の声のまま固定する（ランダムに振り直さない）。稼働中のエンジンの分だけ変換し、
    止まっているエンジンの分は、そのエンジンが使えるようになってからの次の機会に変換する。
    """
    def plan(data: dict, warn: bool) -> list:
        targets = []
        for uid, s in data.items():
            if not isinstance(s, dict) or s.get("voice") is not None:
                continue
            if "voice_index" not in s:
                continue
            eng = engines.get(s.get("engine") or "roid")
            if eng is None:
                continue
            v = _legacy_voice(eng, s.get("voice_index"))
            if v is not None:
                targets.append((uid, eng, v))
            elif warn and uid not in _migration_warned:
                # 番号が範囲外など。推測で割り当てず、そのまま残す
                _migration_warned.add(uid)
                log.warning("移行できません（番号が範囲外）: user=%s engine=%s voice_index=%r → そのまま残します",
                            uid, eng.name, s.get("voice_index"))
        return targets

    settings.refresh()
    if not plan(settings.data, warn=True):
        return 0
    backup = settings.path.with_name(settings.path.name + ".bak-before-migration")
    if settings.path.exists() and not backup.exists():
        with contextlib.suppress(OSError):
            shutil.copy2(settings.path, backup)
            log.info("移行前の設定を %s に保存しました", backup.name)

    def apply(d: dict) -> int:
        # ロックを取ったあとの最新の内容で、もう一度対象を決め直す（他のBOTが先に移行していれば何もしない）
        targets = plan(d, warn=False)
        for uid, eng, v in targets:
            s = d[uid]
            s["engine"], s["voice"], s["auto"] = eng.name, v.key, False
            s.pop("voice_index", None)
            s.pop("unified_index", None)
        return len(targets)

    try:
        n = settings.mutate(apply)
    except (OSError, TimeoutError):
        log.exception("設定の移行結果の保存に失敗")
        return 0
    if n:
        log.info("旧形式の話者設定を %d人分、話者名の形式に移行しました", n)
    return n


def resolve_user_voice(user_id: int) -> tuple[TTSEngine, Voice]:
    s = settings.get(user_id)
    eng_name = s.get("engine")
    eng = engines.get(eng_name)
    if eng:
        v = eng.find_voice(s.get("voice"))
        if v is None and s.get("voice") is None:
            # 旧形式（エンジン内の番号で保存）からの引き継ぎ（通常は起動時に移行済み）
            v = _legacy_voice(eng, s.get("voice_index"))
        if v is not None:
            return eng, v
    # 話者が決まっていない（未設定）か、自動割り当て済みの話者が消えた → 新しく割り当てて保存
    # 自分で選んだ話者が使えないだけ（エンジン停止など）の場合は、保存せずに代役を立てる
    unset = eng_name is None
    stale_auto = bool(s.get("auto")) and eng is not None
    return auto_assign(user_id, persist=unset or stale_auto)


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


# ====================== 読み上げ ======================
@dataclass
class Utterance:
    text: str
    engine: TTSEngine
    voice_key: str
    speed: float
    pitch: float
    message_id: Optional[int] = None   # 元の発言のID（発言が削除されたとき、読み上げを取り消すため）
    cancelled: bool = False


def _remove_quietly(path: str):
    with contextlib.suppress(OSError):
        os.remove(path)
    # A.I.VOICE は、音声と一緒に同じ名前の .txt も保存することがある（Editorの設定）。一緒に消す
    if path.endswith(".wav"):
        with contextlib.suppress(OSError):
            os.remove(path[:-4] + ".txt")


class GuildPlayer:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self.voice_client: Optional[discord.VoiceClient] = None
        self.text_channel_id: Optional[int] = None
        self.voice_channel_id: Optional[int] = None
        self.last_speaker: Optional[tuple[int, float]] = None
        self._tasks: list[asyncio.Task] = []              # 合成係と再生係
        self.current: Optional[Utterance] = None          # いま再生している発言
        self._by_msg: dict[int, Utterance] = {}           # 待ち行列にある発言（削除時に取り消すため）
        self._notice_at: dict[str, float] = {}            # エンジン障害の通知を最後に出した時刻
        # 先読み合成: 再生中に次の発言を合成しておく（合成済みの待ちは最大1件）
        self._slot = asyncio.Semaphore(1)
        self._ready: asyncio.Queue = asyncio.Queue()      # (発言, wavのパス or None, 世代)
        self._epoch = 0                                   # /clear のたびに増やし、古い合成結果を捨てる
        # 待ち行列があふれて読み飛ばした分の通知
        self._dropped = 0
        self._overflow_pending = False
        self._overflow_at = -1e9

    @property
    def active(self) -> bool:
        return self.voice_client is not None and self.voice_client.is_connected()

    # ---- 再起動後に自動で戻るための記録 ----
    def _save_session(self):
        if self.voice_channel_id is None or self.text_channel_id is None:
            return
        sessions_store.data[str(self.guild.id)] = {"vc": self.voice_channel_id, "text": self.text_channel_id}
        try:
            sessions_store.save()
        except OSError:
            log.exception("sessions.json の保存に失敗")

    def _forget_session(self):
        if sessions_store.data.pop(str(self.guild.id), None) is not None:
            try:
                sessions_store.save()
            except OSError:
                log.exception("sessions.json の保存に失敗")

    def set_text_channel(self, channel_id: int):
        self.text_channel_id = channel_id
        self._save_session()

    async def connect(self, channel: discord.VoiceChannel, text_channel_id: int):
        await self.shutdown()
        stale = self.guild.voice_client
        if stale is not None:
            with contextlib.suppress(Exception):
                await stale.disconnect(force=True)
        self.voice_client = await channel.connect()
        self.voice_channel_id = channel.id
        self.text_channel_id = text_channel_id
        self._slot = asyncio.Semaphore(1)
        self._ready = asyncio.Queue()
        self._epoch += 1
        self._tasks = [asyncio.create_task(self._synth_loop()), asyncio.create_task(self._play_loop())]
        self._save_session()
        request_presence_refresh()

    def enqueue(self, utt: Utterance) -> bool:
        try:
            self.queue.put_nowait(utt)
        except asyncio.QueueFull:
            log.info("キューが満杯のため破棄: guild=%s", self.guild.id)
            self._note_overflow()
            return False
        if utt.message_id is not None:
            self._by_msg[utt.message_id] = utt
        return True

    def _send_to_text(self, msg: str):
        """読み上げチャンネルにメッセージを送る（待たずに送る。失敗しても無視）"""
        channel_id = self.text_channel_id
        if channel_id is None:
            return

        async def _send():
            ch = self.guild.get_channel_or_thread(channel_id)
            if ch is not None:
                with contextlib.suppress(Exception):
                    await ch.send(msg)

        spawn(_send())

    def _note_overflow(self):
        """あふれて読み飛ばした分を、少しまとめてから読み上げチャンネルに知らせる（くり返さない）"""
        self._dropped += 1
        if self._overflow_pending or self.text_channel_id is None:
            return
        self._overflow_pending = True
        wait = max(3.0, OVERFLOW_NOTICE_COOLDOWN - (time.monotonic() - self._overflow_at))

        async def _flush():
            try:
                await asyncio.sleep(wait)
            finally:
                self._overflow_pending = False
            n, self._dropped = self._dropped, 0
            self._overflow_at = time.monotonic()
            if n > 0:
                self._send_to_text(f"⚠ 読み上げが追いつかず、{n}件の発言を読み飛ばしました（待てるのは{MAX_QUEUE}件までです）")

        spawn(_flush())

    def cancel_message(self, message_id: int):
        """発言が削除されたとき、その読み上げを取り消す（待ち中なら飛ばし、再生中なら止める）"""
        utt = self._by_msg.get(message_id)
        if utt is None:
            return
        utt.cancelled = True
        if self.current is utt:
            self.skip()

    def _drain(self):
        while not self.queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
        self._by_msg.clear()

    def _notify_engine_trouble(self, engine: TTSEngine):
        """読み上げが連続で失敗したら、読み上げチャンネルに知らせる（同じ通知は一定時間くり返さない）"""
        if engine.fail_streak < FAIL_NOTICE_THRESHOLD or self.text_channel_id is None:
            return
        now = time.monotonic()
        if now - self._notice_at.get(engine.name, -1e9) < FAIL_NOTICE_COOLDOWN:
            return
        self._notice_at[engine.name] = now
        hint = "エンジンが復旧すると自動で再開します。" if ENGINE_AUTO_RECONNECT and engine.can_reconnect else \
            "エンジンが起動しているか確認してください。"
        self._send_to_text(f"⚠ **{engine.display_name}** での読み上げに連続で失敗しています（{engine.fail_streak}回）。{hint}")

    def skip(self):
        vc = self.voice_client
        if vc and vc.is_playing():
            vc.stop()

    def clear(self):
        self._epoch += 1   # 合成済み・合成中の分も捨てる
        self._drain()
        self.skip()

    def _discard_ready(self):
        while True:
            try:
                _utt, wav, _ep = self._ready.get_nowait()
            except asyncio.QueueEmpty:
                return
            if wav:
                _remove_quietly(wav)

    async def shutdown(self, forget: bool = True):
        """
        切断して後片付けする。
        forget=True : 「もう読み上げない」操作（/leave、無人になった、強制切断）。再起動後に自動で戻らない
        forget=False: BOT自身の終了・再起動。再起動後に同じVCへ自動で戻る
        """
        tasks, self._tasks = self._tasks, []
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        vc, self.voice_client = self.voice_client, None
        self.text_channel_id = None
        self.voice_channel_id = None
        self.last_speaker = None
        self.current = None
        self._epoch += 1
        self._drain()
        self._discard_ready()
        if forget:
            self._forget_session()
        if vc is not None:
            with contextlib.suppress(Exception):
                if vc.is_playing():
                    vc.stop()
                await vc.disconnect()
        request_presence_refresh()

    async def _synth_loop(self):
        """待ち行列から1件ずつ取り出して合成する。再生中に次の1件まで先に合成しておく"""
        while True:
            await self._slot.acquire()   # 合成済みで再生待ちの分が1件あるあいだは、次を始めない
            utt: Utterance = await self.queue.get()
            ep = self._epoch
            wav = None
            try:
                vc = self.voice_client
                if not utt.cancelled and vc is not None and vc.is_connected():
                    wav = await utt.engine.synth(utt.text, utt.voice_key, utt.speed, utt.pitch)
            except asyncio.CancelledError:
                raise
            except EngineHungError as e:
                log.warning("読み上げエラー（合成）: %s", e)   # 固まっている間は発言のたびに出るので、1行だけ
                self._notify_engine_trouble(utt.engine)
                wav = None
            except Exception:
                log.exception("読み上げエラー（合成）")
                self._notify_engine_trouble(utt.engine)
                wav = None
            # wav が None（読める文字がない・削除済み・失敗）でも、再生係に渡して枠を返してもらう
            self._ready.put_nowait((utt, wav, ep))

    async def _play_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            utt, wav, ep = await self._ready.get()
            self._slot.release()   # ここから次の発言の合成が始まる（先読み）
            source = None
            try:
                vc = self.voice_client
                if wav is None or ep != self._epoch or utt.cancelled or vc is None or not vc.is_connected():
                    continue   # 合成から再生までの間に、/clear・削除・切断があった分は読まない
                self.current = utt
                volume = guild_settings.get(self.guild.id)["volume"] / 100
                source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(wav), volume=volume)
                done = asyncio.Event()

                def after(error, _done=done):
                    if error:
                        log.error("再生エラー: %s", error)
                    loop.call_soon_threadsafe(_done.set)

                vc.play(source, after=after)
                await done.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("読み上げエラー（再生）")
            finally:
                self.current = None
                if utt.message_id is not None:
                    self._by_msg.pop(utt.message_id, None)
                if source is not None:
                    with contextlib.suppress(Exception):
                        source.cleanup()   # ffmpegを確実に止めてからファイルを消す
                if wav:
                    _remove_quietly(wav)


def get_player(guild: discord.Guild) -> GuildPlayer:
    if guild.id not in players:
        players[guild.id] = GuildPlayer(guild)
    return players[guild.id]


def make_utterance(text: str, user_id: Optional[int], message_id: Optional[int] = None) -> Utterance:
    if user_id is None:
        eng, voice = default_choice()
        return Utterance(text, eng, voice.key, 1.0, 1.0)
    eng, voice = resolve_user_voice(user_id)
    s = settings.get(user_id)
    return Utterance(text, eng, voice.key, float(s["speed"]), float(s["pitch"]), message_id)


_EMOJI_RE = re.compile(
    r"<a?:\w+:\d+>"                                   # Discordのカスタム絵文字
    r"|[0-9#*]️?⃣"                          # キーキャップ絵文字（1️⃣ など）
    "|[\U0001F000-\U0001FAFF"                         # 絵文字・ピクト・国旗・肌色修飾など
    "\U00002600-\U000027BF"                           # その他記号・装飾記号（☺ ❤ ✨ など）
    "\U00002B05-\U00002B07\U00002B1B\U00002B1C\U00002B50\U00002B55"
    "\U0000203C\U00002049\U00002122\U00002139"
    "\U0000231A\U0000231B\U00002328\U000023CF\U000023E9-\U000023F3\U000023F8-\U000023FA"
    "\U000024C2\U000025AA\U000025AB\U000025B6\U000025C0\U000025FB-\U000025FE"
    "\U00002934\U00002935"
    "\U0000FE00-\U0000FE0F\U0000200D\U000020E3"       # 異体字セレクタ・ZWJ・結合キーキャップ
    "\U000E0020-\U000E007F]"                          # 国旗用タグ文字
)


def strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text)


def build_speech_text(message: discord.Message) -> str:
    content = message.content.strip()
    if content.startswith(SKIP_PREFIXES):
        return ""
    text = content
    text = re.sub(r"```.*?```", " コード省略 ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\|\|.*?\|\|", " ネタバレ ", text, flags=re.S)
    text = re.sub(r"https?://\S+", " ユーアールエル ", text)

    users = {m.id: m.display_name for m in message.mentions}
    roles = {r.id: r.name for r in message.role_mentions}
    chans = {c.id: c.name for c in message.channel_mentions}
    text = re.sub(r"<@!?(\d+)>", lambda m: users.get(int(m.group(1)), "メンション"), text)
    text = re.sub(r"<@&(\d+)>", lambda m: roles.get(int(m.group(1)), "ロール"), text)
    text = re.sub(r"<#(\d+)>", lambda m: chans.get(int(m.group(1)), "チャンネル"), text)
    text = strip_emoji(text)                                 # カスタム絵文字・Unicode絵文字は読まない
    text = re.sub(r"(.)\1{4,}", r"\1\1\1", text)             # wwwwwwww や ーーーーー を短縮
    text = re.sub(r"\s+", " ", text).strip()
    text = dictionary.apply(message.guild.id, text)

    # 本文が空（絵文字だけ・スタンプだけ等）のとき、添付があれば「添付ファイル」と読む（画像も同じ）
    # スタンプや絵文字だけの発言は何も読まない
    if not text and message.attachments:
        text = "添付ファイル"
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN] + " 以下略"
    return text


# ====================== 設定パネル（/settings） ======================
SELECT_MAX = 25          # Discordのプルダウンに出せる項目数の上限
DICT_PER_PAGE = 15
VOLUME_STEPS = [10, 25, 50, 75, 90, 100, 110, 125, 150, 175, 200]


def _steps(lo: float, hi: float, step: float = 0.1) -> list[float]:
    n = int(round((hi - lo) / step))
    return [round(lo + step * i, 2) for i in range(n + 1)][:SELECT_MAX]


_KATA_TO_HIRA = {c: c - 0x60 for c in range(ord("ァ"), ord("ヶ") + 1)}


def normalize_search(text: str) -> str:
    """検索用に表記ゆれをそろえる（全角半角・大文字小文字・カタカナ→ひらがな・空白）"""
    t = unicodedata.normalize("NFKC", text).casefold().translate(_KATA_TO_HIRA)
    return re.sub(r"\s+", "", t)


def voice_groups(eng: "TTSEngine") -> list[tuple[str, list[Voice]]]:
    """エンジンの話者を、キャラクターごとにまとめる（並び順はそのまま）。
    「ずんだもん（ノーマル）」「ずんだもん（あまあま）」→ ずんだもん: [ノーマル, あまあま]"""
    groups: dict[str, list[Voice]] = {}
    for v in eng.voices:
        groups.setdefault(_character_name(v.label), []).append(v)
    return list(groups.items())


def style_name(character: str, voice: Voice) -> str:
    """キャラクター名を除いた「話し方」の名前。話し方の名前がなければ「標準」"""
    rest = voice.label[len(character):] if voice.label.startswith(character) else voice.label
    rest = rest.strip()
    if rest.startswith("（") and rest.endswith("）"):
        rest = rest[1:-1]
    return rest or "標準"


def default_style(voices: list[Voice]) -> Voice:
    """キャラクターだけ選んだときの話し方（「ノーマル」があればそれ、なければ最初）"""
    return next((v for v in voices if v.label.endswith("（ノーマル）")), voices[0])


def search_voices(query: str) -> list["UVoice"]:
    """話者名（エンジン名を含む）の一部で探す。名前の頭から一致するものを先に並べる"""
    q = normalize_search(query)
    if not q:
        return list(unified)
    hits = []
    for u in unified:
        name = normalize_search(u.voice.label)
        if q in name:
            hits.append((0 if name.startswith(q) else 1, u))
        elif q in normalize_search(u.label):
            hits.append((2, u))
    hits.sort(key=lambda h: h[0])   # 同じ順位の中では元の並び順のまま
    return [u for _, u in hits]


def check_dict_entry(guild_id: int, word: str, reading: str,
                     old_word: Optional[str] = None) -> tuple[str, str, Optional[str]]:
    """辞書に登録する内容を整えてチェックする。戻り値: (表記, 読み, エラー文。問題なければ None)
    old_word: 編集のとき、元の表記"""
    word, reading = re.sub(r"\s+", " ", word.strip()), reading.strip()
    if not word or not reading:
        return word, reading, "表記と読みを入力してください"
    problem = validate_dict_word(word)
    if problem:
        return word, reading, problem
    if len(word) > DICT_MAX_WORD or len(reading) > DICT_MAX_READING:
        return word, reading, f"長すぎます（表記{DICT_MAX_WORD}文字・読み{DICT_MAX_READING}文字まで）"
    m = dictionary.get_map(guild_id)
    if old_word is not None and old_word in m:
        if word != old_word and word in m:
            return word, reading, f"`{word}` はすでに登録されています"
    elif word not in m and len(m) >= DICT_MAX_ENTRIES:
        return word, reading, f"登録数が上限（{DICT_MAX_ENTRIES}件）です"
    return word, reading, None


class DictEntryModal(discord.ui.Modal):
    def __init__(self, panel: "SettingsPanel", old_word: Optional[str] = None, old_reading: str = ""):
        super().__init__(title="辞書を編集" if old_word else "辞書に登録")
        self.panel = panel
        self.old_word = old_word
        self.word = discord.ui.TextInput(
            label="表記（この文字を…）", default=old_word, max_length=max(1, DICT_MAX_WORD),
        )
        self.reading = discord.ui.TextInput(
            label="読み（…こう読む）", default=old_reading or None, max_length=max(1, DICT_MAX_READING),
        )
        self.add_item(self.word)
        self.add_item(self.reading)

    async def on_submit(self, interaction: discord.Interaction):
        await self.panel.submit_dict(interaction, self.old_word, self.word.value, self.reading.value)


class DictSearchModal(discord.ui.Modal, title="辞書を検索"):
    query = discord.ui.TextInput(label="表記か読みの一部", required=False, max_length=60)

    def __init__(self, panel: "SettingsPanel"):
        super().__init__()
        self.panel = panel
        self.query.default = panel.dict_query or None

    async def on_submit(self, interaction: discord.Interaction):
        self.panel.dict_query = self.query.value.strip()
        self.panel.dict_page = 0
        self.panel.dict_selected = None
        await self.panel.refresh(interaction)


class VoiceSearchModal(discord.ui.Modal, title="話者を検索"):
    query = discord.ui.TextInput(label="話者名の一部（例: ずんだ、きりたん、VOICEVOX）", max_length=40)

    def __init__(self, panel: "SettingsPanel"):
        super().__init__()
        self.panel = panel
        self.query.default = panel.voice_query or None

    async def on_submit(self, interaction: discord.Interaction):
        self.panel.voice_query = self.query.value.strip()
        await self.panel.refresh(interaction)


class SettingsPanel(discord.ui.View):
    """自分の声・辞書・サーバー設定を、プルダウンとボタンで変更する画面（本人にだけ見える）"""
    TABS = {"me": "🎤 自分の声", "dict": "📖 辞書", "server": "⚙ サーバー", "bot": "🤖 BOT"}

    def __init__(self, interaction: discord.Interaction, tab: str = "me"):
        super().__init__(timeout=14 * 60)   # 元の応答を編集できるのは15分まで
        self.origin = interaction
        self.user_id = interaction.user.id
        self.guild = interaction.guild
        self.tab = tab if self._tab_visible(tab) else "me"
        self.engine_filter: Optional[str] = None
        self.voice_page = 0
        self.dict_page = 0
        self.dict_query = ""
        self.dict_selected: Optional[str] = None
        self.notice = ""
        self._voice_opts: dict[str, tuple[str, str]] = {}
        self.voice_query = ""
        self.confirm_restart = False
        self.char_selected: Optional[str] = None   # 話者メニューで選んでいるキャラクター
        self._char_opts: dict[str, tuple[str, str]] = {}

    # ---------- 共通 ----------
    def can_manage(self, user) -> bool:
        if is_owner(user.id):
            return True
        perms = getattr(user, "guild_permissions", None)
        return bool(perms and perms.manage_guild)

    def _button(self, label: str, row: int, callback, style=discord.ButtonStyle.secondary, disabled=False):
        b = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
        b.callback = callback
        self.add_item(b)

    def _select(self, placeholder: str, options: list, row: int, callback):
        s = discord.ui.Select(placeholder=placeholder[:150], options=options[:SELECT_MAX], row=row)
        s.callback = lambda i, _s=s: callback(i, _s.values[0])
        self.add_item(s)

    def _tab_visible(self, tab: str) -> bool:
        if tab in ("dict", "server"):
            return self.guild is not None
        if tab == "bot":
            return is_owner(self.user_id)   # BOTのステータスは、BOTオーナーだけ
        return tab == "me"

    def render(self) -> str:
        self.clear_items()
        for key, label in self.TABS.items():
            if not self._tab_visible(key):
                continue
            active = key == self.tab
            self._button(label, 0, lambda i, k=key: self._switch(i, k),
                         style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
                         disabled=active)
        body = {"me": self._render_me, "dict": self._render_dict, "server": self._render_server,
                "bot": self._render_bot}[self.tab]()
        if self.notice:
            body += f"\n\n{self.notice}"
            self.notice = ""
        return body[:2000]

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=self.render(), view=self)

    async def _switch(self, interaction: discord.Interaction, tab: str):
        self.tab = tab
        self.confirm_restart = False
        await self.refresh(interaction)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("他の人の設定画面です。`/settings` で開いてください", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        with contextlib.suppress(Exception):
            await self.origin.edit_original_response(
                content="設定画面を閉じました（時間切れ）。`/settings` で開き直せます", view=None
            )

    # ---------- 自分の声 ----------
    def _render_me(self) -> str:
        if not engines:
            return "**🎤 自分の声**\nいま使える音声エンジンがありません。エンジンが起動すると、自動で接続します"
        eng, cur = resolve_user_voice(self.user_id)
        s = settings.get(self.user_id)
        if self.engine_filter not in engines or not engines[self.engine_filter].voices:
            self.engine_filter = eng.name
            self.voice_page = None   # 今の話者があるページを開く
        view_eng = engines[self.engine_filter]
        speed, pitch = float(s["speed"]), float(s["pitch"])

        # 行0: タブ＋試し聞き
        self._button("🎧 試し聞き", 0, self._on_test, style=discord.ButtonStyle.success)
        # 行1: 名前で検索（いちばん目立つところ）・速度ピッチ
        searching = bool(self.voice_query)
        self._button("🔍 別の名前で検索" if searching else "🔍 話者を名前で検索", 1, self._on_voice_search,
                     style=discord.ButtonStyle.primary)
        if searching:
            self._button("✖ 検索をやめる", 1, self._on_voice_clear)
        self._button("✏ 速度・ピッチ", 1, self._on_tune)
        self._button("📋 話者一覧", 1, self._on_list)

        guide = ""
        if searching:
            # 行2: 検索結果（全エンジンから）
            hits = search_voices(self.voice_query)
            shown = hits[:SELECT_MAX]
            self._voice_opts = {}
            opts = []
            for i, u in enumerate(shown):
                value = f"v:{i}"
                self._voice_opts[value] = (u.engine.name, u.voice.key)
                is_cur = u.engine is eng and u.voice.key == cur.key
                opts.append(discord.SelectOption(
                    label=u.voice.label[:100], value=value, default=is_cur,
                    description=f"{u.engine.display_name}" + ("・いまの声" if is_cur else ""),
                ))
            if hits:
                self._select(f"🔍「{self.voice_query}」の結果から選ぶ（{len(hits)}件）", opts, 2, self._on_voice)
                guide = f"🔍 「**{self.voice_query}**」の検索結果：**{len(hits)}件** ↓ 下のメニューから選んでください"
                if len(hits) > len(shown):
                    guide += f"\n-# 多いので{len(shown)}件だけ出しています。言葉を足して絞ってください"
            else:
                guide = (f"🔍 「**{self.voice_query}**」は見つかりませんでした\n"
                         "-# ひらがな・カタカナは区別しません。短くするか、別の名前で試してください")
        else:
            # 行2: エンジン、行3: キャラクター、行4: 話し方
            opts = [
                discord.SelectOption(label=e.display_name, value=e.name, default=e.name == view_eng.name,
                                     description=f"{len(voice_groups(e))}人・{len(e.voices)}話者")
                for key in ENGINE_ORDER if (e := engines.get(key)) and e.voices
            ]
            self._select("エンジンを選ぶ", opts, 2, self._on_engine)
            self._render_characters(eng, cur, view_eng)
            guide = "-# 下のメニューで選ぶと、すぐに保存されます。名前がわかるなら「🔍 話者を名前で検索」が早いです"

        auto = "\n-# 自動で割り当てた声です。好きな声を選ぶと、そちらに変わります" if s.get("auto") else ""
        return (
            "**🎤 自分の声**（全サーバー共通）\n"
            f"🗣 いまの声：**{cur.label}**　`{eng.display_name}`\n"
            f"速度 **{speed}**・ピッチ **{pitch}**{auto}\n\n"
            + guide
        )

    def _render_characters(self, eng, cur, view_eng):
        groups = voice_groups(view_eng)
        names = [n for n, _ in groups]
        cur_char = _character_name(cur.label) if view_eng is eng else None
        if self.char_selected not in names:
            self.char_selected = cur_char
            self.voice_page = None   # 選んでいるキャラクターがあるページを開く

        # 行3: キャラクター（多いときはページ送り。そのときは「前へ」「次へ」で2枠使う）
        paged = len(groups) > SELECT_MAX
        size = SELECT_MAX - 2 if paged else SELECT_MAX
        pages = max(1, (len(groups) + size - 1) // size)
        if self.voice_page is None:
            self.voice_page = (names.index(self.char_selected) if self.char_selected in names else 0) // size
        self.voice_page = max(0, min(self.voice_page, pages - 1))
        start = self.voice_page * size
        self._char_opts = {}
        opts = []
        if paged and self.voice_page > 0:
            opts.append(discord.SelectOption(label="◀ 前のページへ", value="nav:prev",
                                             description=f"{self.voice_page}/{pages}ページ目に戻る"))
        for i, (name, vs) in enumerate(groups[start:start + size], start):
            value = f"c:{i}"
            self._char_opts[value] = (view_eng.name, name)
            desc = [f"{len(vs)}種類の話し方"] if len(vs) > 1 else []
            if name == cur_char:
                desc.append("いまの声")
            opts.append(discord.SelectOption(label=name[:100], value=value, default=name == self.char_selected,
                                             description="・".join(desc) or None))
        if paged and self.voice_page < pages - 1:
            opts.append(discord.SelectOption(label="次のページへ ▶", value="nav:next",
                                             description=f"{self.voice_page + 2}/{pages}ページ目へ"))
        page_note = f"・{self.voice_page + 1}/{pages}ページ" if paged else ""
        self._select(f"キャラクターを選ぶ（{view_eng.display_name}・{len(groups)}人{page_note}）",
                     opts, 3, self._on_character)

        # 行4: 話し方（選んだキャラクターに2つ以上あるときだけ）
        styles = dict(groups).get(self.char_selected) or []
        self._voice_opts = {}
        if len(styles) > 1:
            opts = []
            for i, v in enumerate(styles[:SELECT_MAX]):
                value = f"v:{i}"
                self._voice_opts[value] = (view_eng.name, v.key)
                is_cur = view_eng is eng and v.key == cur.key
                opts.append(discord.SelectOption(label=style_name(self.char_selected, v)[:100], value=value,
                                                 default=is_cur, description="いまの声" if is_cur else None))
            self._select(f"話し方を選ぶ（{self.char_selected}・{len(styles)}種類）", opts, 4, self._on_voice)

    async def _on_engine(self, interaction: discord.Interaction, value: str):
        self.engine_filter = value
        self.voice_page = 0
        self.voice_query = ""
        self.char_selected = None
        self.notice = "下のメニューからキャラクターを選んでください"
        await self.refresh(interaction)

    async def _on_character(self, interaction: discord.Interaction, value: str):
        if value in ("nav:prev", "nav:next"):
            self.voice_page = (self.voice_page or 0) + (1 if value == "nav:next" else -1)
            await self.refresh(interaction)
            return
        hit = self._char_opts.get(value)
        eng = engines.get(hit[0]) if hit else None
        styles = dict(voice_groups(eng)).get(hit[1]) if eng else None
        if not styles:
            self.notice = "⚠ そのキャラクターは今は使えません。選び直してください"
        else:
            name = hit[1]
            self.char_selected = name
            ceng, cv = resolve_user_voice(self.user_id)
            if ceng is eng and _character_name(cv.label) == name:
                self.notice = f"いまも **{name}** です" + ("。話し方は下のメニューで変えられます" if len(styles) > 1 else "")
            else:
                v = default_style(styles)
                settings.update(self.user_id, engine=eng.name, voice=v.key, auto=False)
                if len(styles) > 1:
                    self.notice = (f"✅ **{name}**（{style_name(name, v)}）にしました。"
                                   f"話し方（{len(styles)}種類）は、いちばん下のメニューで変えられます")
                else:
                    self.notice = f"✅ 話者を **[{eng.display_name}] {name}** にしました"
        await self.refresh(interaction)

    async def _on_list(self, interaction: discord.Interaction):
        await open_catalog(interaction)

    async def _on_voice_search(self, interaction: discord.Interaction):
        await interaction.response.send_modal(VoiceSearchModal(self))

    async def _on_voice_clear(self, interaction: discord.Interaction):
        self.voice_query = ""
        self.voice_page = None   # 今の話者があるページに戻す
        self.engine_filter = None
        self.char_selected = None
        await self.refresh(interaction)

    async def _on_voice(self, interaction: discord.Interaction, value: str):
        """話し方のメニュー・検索結果から、話者を1つ選んだとき"""
        hit = self._voice_opts.get(value)
        eng = engines.get(hit[0]) if hit else None
        v = eng.find_voice(hit[1]) if eng else None
        if v is None:
            self.notice = "⚠ その話者は今は使えません。選び直してください"
        else:
            settings.update(self.user_id, engine=eng.name, voice=v.key, auto=False)
            self.notice = f"✅ 話者を **[{eng.display_name}] {v.label}** にしました"
        await self.refresh(interaction)

    async def _on_tune(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TuneModal(self))

    async def _on_test(self, interaction: discord.Interaction):
        await send_private_test(interaction)   # VCには流さず、本人にだけ音声ファイルで送る

    # ---------- 辞書 ----------
    def _dict_entries(self) -> list[tuple[str, str]]:
        items = sorted(dictionary.get_map(self.guild.id).items())
        q = self.dict_query.casefold()
        if q:
            items = [(w, r) for w, r in items if q in w.casefold() or q in r.casefold()]
        return items

    def _render_dict(self) -> str:
        m = dictionary.get_map(self.guild.id)
        items = self._dict_entries()
        pages = max(1, (len(items) + DICT_PER_PAGE - 1) // DICT_PER_PAGE)
        self.dict_page = max(0, min(self.dict_page, pages - 1))
        shown = items[self.dict_page * DICT_PER_PAGE:(self.dict_page + 1) * DICT_PER_PAGE]
        if self.dict_selected not in m:
            self.dict_selected = None

        if shown:
            self._select("編集・削除する語を選ぶ", [
                discord.SelectOption(label=w[:100], value=w, description=f"→ {r}"[:100],
                                     default=w == self.dict_selected)
                for w, r in shown
            ], 1, self._on_dict_pick)
        sel = self.dict_selected is None
        self._button("➕ 追加", 2, self._on_dict_add, style=discord.ButtonStyle.success,
                     disabled=len(m) >= DICT_MAX_ENTRIES)
        self._button("✏ 編集", 2, self._on_dict_edit, disabled=sel)
        self._button("🗑 削除", 2, self._on_dict_delete, style=discord.ButtonStyle.danger, disabled=sel)
        self._button("◀", 2, lambda i: self._dict_move(i, -1), disabled=self.dict_page <= 0)
        self._button("▶", 2, lambda i: self._dict_move(i, 1), disabled=self.dict_page >= pages - 1)
        self._button("🔍 検索", 3, self._on_dict_search)
        if self.dict_query:
            self._button("✖ 検索をやめる", 3, self._on_dict_clear_search)

        filt = f"｜検索: `{self.dict_query}`（{len(items)}件）" if self.dict_query else ""
        lines = [
            f"**📖 辞書**（このサーバー共通・{len(m)}/{DICT_MAX_ENTRIES}件）{filt}｜{self.dict_page + 1}/{pages}ページ",
        ]
        for w, r in shown:
            mark = "▶ " if w == self.dict_selected else "・"
            warn = "　⚠使われません（登録できない語です）" if validate_dict_word(w) else ""
            lines.append(f"{mark}`{w}` → `{r}`{warn}")
        if not shown:
            lines.append("（該当なし）" if self.dict_query else "（まだ登録がありません。「➕ 追加」から登録できます）")
        return "\n".join(lines)

    async def _dict_move(self, interaction: discord.Interaction, delta: int):
        self.dict_page += delta
        self.dict_selected = None
        await self.refresh(interaction)

    async def _on_dict_pick(self, interaction: discord.Interaction, value: str):
        self.dict_selected = value
        await self.refresh(interaction)

    async def _on_dict_add(self, interaction: discord.Interaction):
        await interaction.response.send_modal(DictEntryModal(self))

    async def _on_dict_edit(self, interaction: discord.Interaction):
        w = self.dict_selected
        r = dictionary.get_map(self.guild.id).get(w) if w else None
        if r is None:
            self.notice = "⚠ その語は、もう登録されていません"
            await self.refresh(interaction)
            return
        await interaction.response.send_modal(DictEntryModal(self, w, r))

    async def _on_dict_delete(self, interaction: discord.Interaction):
        w = self.dict_selected
        if w and dictionary.remove(self.guild.id, w):
            self.notice = f"🗑 削除しました: `{w}`"
            log.info("[DICT] guild=%s user=%s 削除: %s", self.guild.id, self.user_id, w)
        else:
            self.notice = "⚠ その語は、もう登録されていません"
        self.dict_selected = None
        await self.refresh(interaction)

    async def _on_dict_search(self, interaction: discord.Interaction):
        await interaction.response.send_modal(DictSearchModal(self))

    async def _on_dict_clear_search(self, interaction: discord.Interaction):
        self.dict_query = ""
        self.dict_page = 0
        await self.refresh(interaction)

    async def submit_dict(self, interaction: discord.Interaction, old_word: Optional[str], word: str, reading: str):
        word, reading, err = check_dict_entry(self.guild.id, word, reading, old_word)
        if err:
            self.notice = f"⚠ {err}"
        else:
            if old_word is not None and old_word in dictionary.get_map(self.guild.id):
                dictionary.replace(self.guild.id, old_word, word, reading)
                self.notice = f"✅ 変更しました: `{word}` → `{reading}`"
            else:
                dictionary.add(self.guild.id, word, reading)
                self.notice = f"✅ 登録しました: `{word}` → `{reading}`"
            log.info("[DICT] guild=%s user=%s 登録: %s → %s", self.guild.id, self.user_id, word, reading)
            self.dict_selected = word
            # 登録した語のあるページへ
            keys = [w for w, _ in self._dict_entries()]
            if word in keys:
                self.dict_page = keys.index(word) // DICT_PER_PAGE
        await self.refresh(interaction)

    # ---------- サーバー ----------
    def _render_server(self) -> str:
        g = guild_settings.get(self.guild.id)
        vol, rn, an = int(g["volume"]), bool(g["read_name"]), bool(g["announce"])
        text = (
            "**⚙ サーバー設定**（このサーバー共通）\n"
            f"音量: **{vol}%**\n"
            f"名前の読み上げ: **{'ON' if rn else 'OFF'}**　（「〇〇、本文」と読む）\n"
            f"入退室の読み上げ: **{'ON' if an else 'OFF'}**"
        )
        if not self.can_manage(self.origin.user):
            return text + "\n-# 変更には「サーバー管理」の権限が必要です"
        self._button("✏ まとめて変える", 1, self._on_server_edit, style=discord.ButtonStyle.primary)
        self._button("📌 このチャンネルに設定ボタンを置く", 1, self._on_place_launcher)
        return text + "\n-# 「📌」を押すと、誰でも押せる「設定を開く」メッセージをこのチャンネルに置きます"

    async def _guard_manage(self, interaction: discord.Interaction) -> bool:
        if self.can_manage(interaction.user):
            return True
        self.notice = "⚠ 変更には「サーバー管理」の権限が必要です"
        await self.refresh(interaction)
        return False

    async def _on_server_edit(self, interaction: discord.Interaction):
        if await self._guard_manage(interaction):
            await interaction.response.send_modal(ServerModal(self))

    async def _on_place_launcher(self, interaction: discord.Interaction):
        if not await self._guard_manage(interaction):
            return
        try:
            await interaction.channel.send(embed=launcher_embed(), view=SettingsLauncher())
            self.notice = "📌 このチャンネルに「設定を開く」メッセージを置きました（ピン留めがおすすめです）"
            log.info("[SETTINGS] guild=%s user=%s 設定ボタンを設置: channel=%s",
                     self.guild.id, self.user_id, interaction.channel_id)
        except discord.Forbidden:
            self.notice = "⚠ このチャンネルに書き込む権限がBOTにありません"
        except Exception:
            log.exception("設定ボタンの設置に失敗")
            self.notice = "⚠ 置けませんでした"
        await self.refresh(interaction)

    # ---------- BOT（オーナーのみ） ----------
    def _render_bot(self) -> str:
        if self.confirm_restart:
            self._button("🔄 はい、再起動する", 1, self._on_restart_yes, style=discord.ButtonStyle.danger)
            self._button("やめる", 1, self._on_restart_no)
            return (
                "**🤖 BOT**\n"
                "⚠ **本当に再起動しますか？**\n"
                "接続中のVCからいったん抜けて、再起動のあと自動で戻ります。読み上げ待ちの分は読まれません。"
            )
        self._button("✏ 表示をまとめて変える", 1, self._on_bot_edit, style=discord.ButtonStyle.primary)
        self._button("🔄 再起動", 1, self._on_restart, style=discord.ButtonStyle.danger)
        return (
            "**🤖 BOT**（BOTオーナーだけに出ます・全サーバー共通）\n"
            + describe_presence() + "\n"
            "-# 自動表示がONのときは「〇サーバーで読み上げ中」などを自動で出します"
        )

    async def _guard_owner(self, interaction: discord.Interaction) -> bool:
        if is_owner(interaction.user.id):
            return True
        self.notice = "⚠ BOTオーナーだけが使えます"
        await self.refresh(interaction)
        return False

    async def _on_bot_edit(self, interaction: discord.Interaction):
        if await self._guard_owner(interaction):
            await interaction.response.send_modal(PresenceModal(self))

    async def _on_restart(self, interaction: discord.Interaction):
        if await self._guard_owner(interaction):
            self.confirm_restart = True
            await self.refresh(interaction)

    async def _on_restart_no(self, interaction: discord.Interaction):
        self.confirm_restart = False
        await self.refresh(interaction)

    async def _on_restart_yes(self, interaction: discord.Interaction):
        if not await self._guard_owner(interaction):
            return
        self.confirm_restart = False
        self.stop()
        await interaction.response.edit_message(content="🔄 再起動します…", view=None)
        await request_restart(interaction.user.id)


# ---------- ポップアップ（discord.py 2.6 以上：ポップアップの中にプルダウンを置く） ----------
def _modal_select(choices: list[tuple[str, str]], current: str) -> discord.ui.Select:
    """ポップアップ用のプルダウン。choices: (表示名, 値)。current を選んだ状態で開く"""
    return discord.ui.Select(options=[
        discord.SelectOption(label=label, value=value, default=value == current)
        for label, value in choices[:SELECT_MAX]
    ])


def _picked(select: discord.ui.Select, fallback: str) -> str:
    return select.values[0] if select.values else fallback


def _num_choices(steps: list[float], name: str) -> list[tuple[str, str]]:
    return [(f"{name} {v}" + ("（標準）" if v == 1.0 else ""), str(v)) for v in steps]


def _num_current(steps: list[float], value: float) -> str:
    """いまの値にいちばん近い選択肢（0.1刻みでない値が保存されていた場合のため）"""
    return str(min(steps, key=lambda v: abs(v - value)))


ON_OFF = [("ON（読む）", "on"), ("OFF（読まない）", "off")]


class TuneModal(discord.ui.Modal, title="速度・ピッチ"):
    def __init__(self, panel: SettingsPanel):
        super().__init__()
        self.panel = panel
        s = settings.get(panel.user_id)
        sp, pt = _steps(SPEED_MIN, SPEED_MAX), _steps(PITCH_MIN, PITCH_MAX)
        self.cur_speed, self.cur_pitch = _num_current(sp, float(s["speed"])), _num_current(pt, float(s["pitch"]))
        self.speed = _modal_select(_num_choices(sp, "速度"), self.cur_speed)
        self.pitch = _modal_select(_num_choices(pt, "ピッチ"), self.cur_pitch)
        self.add_item(discord.ui.Label(text="速度", description="1.0が標準。大きいほど速い", component=self.speed))
        self.add_item(discord.ui.Label(text="ピッチ", description="1.0が標準。大きいほど高い", component=self.pitch))

    async def on_submit(self, interaction: discord.Interaction):
        speed, pitch = float(_picked(self.speed, self.cur_speed)), float(_picked(self.pitch, self.cur_pitch))
        settings.update(self.panel.user_id, speed=speed, pitch=pitch)
        self.panel.notice = f"✅ 速度 **{speed}**・ピッチ **{pitch}** にしました"
        await self.panel.refresh(interaction)


class ServerModal(discord.ui.Modal, title="サーバー設定"):
    def __init__(self, panel: SettingsPanel):
        super().__init__()
        self.panel = panel
        g = guild_settings.get(panel.guild.id)
        vol = int(g["volume"])
        self.cur = (str(vol), "on" if g["read_name"] else "off", "on" if g["announce"] else "off")
        steps = sorted(set(VOLUME_STEPS) | {vol})
        self.volume = _modal_select([(f"音量 {v}%" + ("（標準）" if v == 100 else ""), str(v)) for v in steps],
                                    self.cur[0])
        self.read_name = _modal_select(ON_OFF, self.cur[1])
        self.announce = _modal_select(ON_OFF, self.cur[2])
        self.add_item(discord.ui.Label(text="音量", description="このサーバー共通。100%が標準", component=self.volume))
        self.add_item(discord.ui.Label(text="名前の読み上げ", description="ONにすると「〇〇、本文」と読みます",
                                       component=self.read_name))
        self.add_item(discord.ui.Label(text="入退室の読み上げ", description="VCに入った・出た人を読み上げます",
                                       component=self.announce))

    async def on_submit(self, interaction: discord.Interaction):
        if not self.panel.can_manage(interaction.user):
            self.panel.notice = "⚠ 変更には「サーバー管理」の権限が必要です"
        else:
            vol = int(_picked(self.volume, self.cur[0]))
            rn = _picked(self.read_name, self.cur[1]) == "on"
            an = _picked(self.announce, self.cur[2]) == "on"
            guild_settings.update(self.panel.guild.id, volume=vol, read_name=rn, announce=an)
            self.panel.notice = "✅ サーバー設定を保存しました"
            log.info("[SETTINGS] guild=%s user=%s 音量=%s 名前=%s 入退室=%s",
                     self.panel.guild.id, interaction.user.id, vol, rn, an)
        await self.panel.refresh(interaction)


class PresenceModal(discord.ui.Modal, title="BOTの表示"):
    def __init__(self, panel: SettingsPanel):
        super().__init__()
        self.panel = panel
        status, kind, text = stored_presence()
        self.cur = (status, kind, "on" if presence_auto() else "off")
        self.status = _modal_select([(label, key) for key, label in STATUS_LABELS.items()], status)
        self.kind = _modal_select([(f"{label}（{key}）", key) for key, label in ACTIVITY_LABELS.items()], kind)
        self.text = discord.ui.TextInput(default=text, required=False, max_length=ACTIVITY_MAX_LEN,
                                         placeholder="例: /join で読み上げ")
        self.auto = _modal_select([("ON", "on"), ("OFF", "off")], self.cur[2])
        self.add_item(discord.ui.Label(text="ステータス", component=self.status))
        self.add_item(discord.ui.Label(text="表示の種類", component=self.kind))
        self.add_item(discord.ui.Label(text="表示する文字", description="自動表示がOFFのときに出します。空なら何も出しません",
                                       component=self.text))
        self.add_item(discord.ui.Label(text="自動表示", description="ONなら「〇サーバーで読み上げ中」などを自動で出します",
                                       component=self.auto))

    async def on_submit(self, interaction: discord.Interaction):
        if not is_owner(interaction.user.id):
            self.panel.notice = "⚠ BOTオーナーだけが使えます"
        else:
            err = await change_presence(
                status=_picked(self.status, self.cur[0]),
                kind=_picked(self.kind, self.cur[1]),
                activity=self.text.value,
                auto=_picked(self.auto, self.cur[2]) == "on",
            )
            self.panel.notice = f"⚠ {err}" if err else "✅ BOTの表示を変えました"
        await self.panel.refresh(interaction)


TEST_TEXT = "テストです。この声で読み上げます"
TEST_COOLDOWN_SECONDS = 5
_test_last: dict[int, float] = {}


async def send_private_test(interaction: discord.Interaction, sample: Optional[tuple] = None):
    """合成した音声を、本人にだけ見えるメッセージで送る（VCには流さない）。
    sample=(エンジン, 話者) を渡すと、自分の声を変えずにその声で試し聞きする（話者カタログから）"""
    uid = interaction.user.id
    now = time.monotonic()
    if now - _test_last.get(uid, 0) < TEST_COOLDOWN_SECONDS:
        await interaction.response.send_message("少し待ってから、もう一度押してください", ephemeral=True)
        return
    if not engines:
        await interaction.response.send_message("いま使える音声エンジンがありません", ephemeral=True)
        return
    _test_last[uid] = now
    await interaction.response.defer(ephemeral=True, thinking=True)   # 合成に3秒以上かかっても失敗しないように
    path = None
    try:
        eng, voice = sample if sample else resolve_user_voice(uid)
        s = settings.get(uid)
        path = await eng.synth(TEST_TEXT, voice.key, float(s["speed"]), float(s["pitch"]))
        if not path:
            await interaction.followup.send("⚠ 合成できませんでした", ephemeral=True)
            return
        cur_eng, cur_voice = resolve_user_voice(uid)
        changed = "" if (cur_eng is eng and cur_voice.key == voice.key) else "まだ声は変わっていません。"
        await interaction.followup.send(
            f"🎧 **[{eng.display_name}] {voice.label}**（速度 {s['speed']}・ピッチ {s['pitch']}）\n"
            f"-# あなたにだけ見えています。{changed}▶ を押すと再生します",
            file=discord.File(path, filename="test_voice.wav"),
            ephemeral=True,
        )
    except Exception as e:
        log.exception("試し聞きの合成に失敗")
        with contextlib.suppress(Exception):
            await interaction.followup.send(f"⚠ 合成に失敗しました（{type(e).__name__}）", ephemeral=True)
    finally:
        if path:
            _remove_quietly(path)


CATALOG_PER_PAGE = 15   # 1ページに並べるキャラクター（3列×5行）


class VoiceCatalog(discord.ui.View):
    """📋 話者一覧（話者カタログ）。エンジンごとにキャラクターを並べ、選ぶと詳しい画面になる。
    自分の声を変えずに試し聞きでき、気に入ったら「この声にする」で決める（本人にだけ見える）"""

    def __init__(self, interaction: discord.Interaction):
        super().__init__(timeout=14 * 60)
        self.origin = interaction
        self.user_id = interaction.user.id
        self.engine_name: Optional[str] = None
        self.page: Optional[int] = None
        self.char: Optional[str] = None        # 詳しい画面で見ているキャラクター（None なら一覧）
        self.voice_key: Optional[str] = None   # 詳しい画面で選んでいる話し方
        self.notice = ""
        self._chars: dict[str, str] = {}

    # ---------- 共通 ----------
    def _button(self, label, row, callback, style=discord.ButtonStyle.secondary, disabled=False):
        b = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
        b.callback = callback
        self.add_item(b)

    def _current(self) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """いまの自分の声 (エンジン名, キャラクター名, 話者キー)"""
        with contextlib.suppress(Exception):
            eng, v = resolve_user_voice(self.user_id)
            return eng.name, _character_name(v.label), v.key
        return None, None, None

    def render(self) -> tuple[str, discord.Embed]:
        self.clear_items()
        avail = [e for key in ENGINE_ORDER if (e := engines.get(key)) and e.voices]
        if not avail:
            return "", discord.Embed(title="📋 話者一覧", description="いま使える音声エンジンがありません")
        cur_eng, cur_char, cur_key = self._current()
        if self.engine_name not in [e.name for e in avail]:
            self.engine_name = cur_eng if cur_eng in [e.name for e in avail] else avail[0].name
            self.page, self.char = None, None
        eng = engines[self.engine_name]
        groups = voice_groups(eng)
        if self.char is not None and self.char not in dict(groups):
            self.char = None
        embed = self._render_detail(eng, groups, cur_eng, cur_key) if self.char else \
            self._render_list(eng, groups, avail, cur_eng, cur_char)
        notice, self.notice = self.notice, ""
        return notice, embed

    async def refresh(self, interaction: discord.Interaction):
        content, embed = self.render()
        await interaction.response.edit_message(content=content or None, embed=embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("他の人の一覧です。`/settings` の「📋 話者一覧」で開いてください",
                                                    ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        with contextlib.suppress(Exception):
            await self.origin.edit_original_response(
                content="話者一覧を閉じました（時間切れ）。`/settings` の「📋 話者一覧」で開き直せます", view=None)

    # ---------- 一覧 ----------
    def _render_list(self, eng, groups, avail, cur_eng, cur_char) -> discord.Embed:
        pages = max(1, (len(groups) + CATALOG_PER_PAGE - 1) // CATALOG_PER_PAGE)
        if self.page is None:
            names = [n for n, _ in groups]
            idx = names.index(cur_char) if (eng.name == cur_eng and cur_char in names) else 0
            self.page = idx // CATALOG_PER_PAGE
        self.page = max(0, min(self.page, pages - 1))
        shown = groups[self.page * CATALOG_PER_PAGE:(self.page + 1) * CATALOG_PER_PAGE]

        cells = []
        for name, vs in shown:
            text = name + (f" ({len(vs)})" if len(vs) > 1 else "")
            cells.append(f"**★ {text}**" if (eng.name == cur_eng and name == cur_char) else text)
        head = f"{len(groups)}人・{len(eng.voices)}話者" + (f"｜{self.page + 1}/{pages}ページ" if pages > 1 else "")
        e = discord.Embed(title=f"📋 話者一覧 ─ {eng.display_name}", color=0x5865F2,
                          description=head + "\n\n" + "\n".join(f"- {c}" for c in cells))   # 箇条書き
        foot = "（ ）の数字は話し方の数です。下のメニューで選ぶと、試し聞きできます"
        if eng.name == cur_eng:
            foot = "★ は、いまのあなたの声です。" + foot
        e.set_footer(text=foot)

        # 行0: エンジン
        for x in avail[:5]:
            self._button(x.display_name, 0, lambda i, n=x.name: self._on_engine(i, n),
                         style=discord.ButtonStyle.primary if x is eng else discord.ButtonStyle.secondary,
                         disabled=x is eng)
        # 行1: キャラクター
        self._chars = {}
        opts = []
        for i, (name, vs) in enumerate(shown):
            value = f"c:{i}"
            self._chars[value] = name
            desc = [f"{len(vs)}種類の話し方"] if len(vs) > 1 else []
            if eng.name == cur_eng and name == cur_char:
                desc.append("いまの声")
            opts.append(discord.SelectOption(label=name[:100], value=value, description="・".join(desc) or None))
        s = discord.ui.Select(placeholder="気になるキャラを選ぶ（試し聞きできます）", options=opts, row=1)
        s.callback = lambda i, _s=s: self._on_char(i, _s.values[0])
        self.add_item(s)
        # 行2: ページ送り
        if pages > 1:
            self._button("◀ 前", 2, lambda i: self._on_page(i, -1), disabled=self.page <= 0)
            self._button("次 ▶", 2, lambda i: self._on_page(i, 1), disabled=self.page >= pages - 1)
        return e

    async def _on_engine(self, interaction, name):
        self.engine_name, self.page, self.char = name, 0, None
        await self.refresh(interaction)

    async def _on_page(self, interaction, delta):
        self.page = (self.page or 0) + delta
        await self.refresh(interaction)

    async def _on_char(self, interaction, value):
        self.char = self._chars.get(value)
        self.voice_key = None
        await self.refresh(interaction)

    # ---------- キャラクターの詳しい画面 ----------
    def _render_detail(self, eng, groups, cur_eng, cur_key) -> discord.Embed:
        styles = dict(groups)[self.char]
        keys = [v.key for v in styles]
        if self.voice_key not in keys:
            # いまの声のキャラなら、いまの話し方。そうでなければ「ノーマル」
            self.voice_key = cur_key if (eng.name == cur_eng and cur_key in keys) else default_style(styles).key
        voice = next(v for v in styles if v.key == self.voice_key)
        is_cur = eng.name == cur_eng and voice.key == cur_key

        desc = eng.display_name + (f"・話し方 {len(styles)}種類" if len(styles) > 1 else "")
        if is_cur:
            desc += "\n✅ いまのあなたの声です"
        e = discord.Embed(title=f"🗣 {self.char}", color=0x5865F2, description=desc)
        if len(styles) > 1:
            e.add_field(name="話し方", inline=False, value="・".join(
                f"**[{style_name(self.char, v)}]**" if v.key == voice.key else style_name(self.char, v) for v in styles))
        if eng.name == "voicevox":
            e.set_footer(text=f"クレジット表記：VOICEVOX:{VOICEVOX_CREDIT_NAMES.get(self.char, self.char)}")

        # 行0: 話し方
        if len(styles) > 1:
            opts = [discord.SelectOption(label=style_name(self.char, v)[:100], value=v.key, default=v.key == voice.key,
                                         description="いまの声" if (eng.name == cur_eng and v.key == cur_key) else None)
                    for v in styles[:SELECT_MAX]]
            s = discord.ui.Select(placeholder="話し方を選ぶ", options=opts, row=0)
            s.callback = lambda i, _s=s: self._on_style(i, _s.values[0])
            self.add_item(s)
        # 行1: 試し聞き・決定・戻る
        self._button("🎧 この声で試し聞き", 1, self._on_sample, style=discord.ButtonStyle.primary)
        self._button("✅ いまの声です" if is_cur else "✅ この声にする", 1, self._on_choose,
                     style=discord.ButtonStyle.success, disabled=is_cur)
        self._button("◀ 一覧に戻る", 1, self._on_back)
        return e

    def _selected(self):
        eng = engines.get(self.engine_name)
        v = eng.find_voice(self.voice_key) if eng else None
        return (eng, v) if v else None

    async def _on_style(self, interaction, key):
        self.voice_key = key
        await self.refresh(interaction)

    async def _on_sample(self, interaction):
        sel = self._selected()
        if sel is None:
            self.notice = "⚠ その声は今は使えません"
            await self.refresh(interaction)
            return
        await send_private_test(interaction, sample=sel)

    async def _on_choose(self, interaction):
        sel = self._selected()
        if sel is None:
            self.notice = "⚠ その声は今は使えません"
        else:
            eng, v = sel
            settings.update(self.user_id, engine=eng.name, voice=v.key, auto=False)
            self.notice = f"✅ あなたの声を **[{eng.display_name}] {v.label}** にしました"
        await self.refresh(interaction)

    async def _on_back(self, interaction):
        self.char = None
        await self.refresh(interaction)


async def open_catalog(interaction: discord.Interaction):
    view = VoiceCatalog(interaction)
    content, embed = view.render()
    await interaction.response.send_message(content=content or None, embed=embed, view=view, ephemeral=True)


class SettingsLauncher(discord.ui.View):
    """チャンネルに置いておく「設定を開く」メッセージ。BOTを再起動しても押せる（custom_id 固定・期限なし）"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎤 声を変える", style=discord.ButtonStyle.primary, custom_id="ttsset:me")
    async def open_me(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_settings(interaction, "me")

    @discord.ui.button(label="🎧 試し聞き", style=discord.ButtonStyle.success, custom_id="ttsset:test")
    async def open_test(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_private_test(interaction)

    @discord.ui.button(label="📖 辞書", style=discord.ButtonStyle.secondary, custom_id="ttsset:dict")
    async def open_dict(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_settings(interaction, "dict")

    @discord.ui.button(label="⚙ サーバー設定", style=discord.ButtonStyle.secondary, custom_id="ttsset:server")
    async def open_server(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_settings(interaction, "server")

    @discord.ui.button(label="📋 話者一覧", style=discord.ButtonStyle.secondary, custom_id="ttsset:list")
    async def open_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_catalog(interaction)


def launcher_embed() -> discord.Embed:
    e = discord.Embed(
        title="🔊 読み上げBOTの設定",
        description=(
            "ボタンを押すと、**あなた専用の設定画面**が開きます（ほかの人には見えません）。\n\n"
            "🎤 **声を変える** … 話者・速度・ピッチ（名前で検索もできます）\n"
            "🎧 **試し聞き** … いまの自分の声を、自分だけで聞けます\n"
            "📖 **辞書** … 読み方の登録・編集（サーバー共通）\n"
            "⚙ **サーバー設定** … 音量・名前や入退室の読み上げ（変更は管理者のみ）\n"
            "📋 **話者一覧** … 使えるキャラクターを、エンジンごとに一覧で見られます"
        ),
        color=0x5865F2,
    )
    e.set_footer(text="読み上げを始めるには、VCに入って /join")
    return e


async def open_settings(interaction: discord.Interaction, tab: str = "me"):
    view = SettingsPanel(interaction, tab)
    await interaction.response.send_message(content=view.render(), view=view, ephemeral=True)


# ====================== Bot ======================
class TTSBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        status, kind, text = effective_presence()
        super().__init__(
            intents=intents,
            status=STATUS_MAP[status],
            activity=build_activity(kind, text),
        )
        self.tree = app_commands.CommandTree(self)
        self._cleaned_up = False
        self._closing = False                       # BOT自身の終了中（このあいだの切断は「ユーザーの操作」ではない）
        self._restored = False                      # 自動復帰を済ませたか（on_ready は何度も呼ばれるため）
        self._presence_event = asyncio.Event()
        self._last_presence = (status, kind, text)
        self._tasks: list[asyncio.Task] = []
        self.retry_state: dict[str, dict] = {}      # エンジンごとの再接続の待ち時間
        self._rejoining: set[int] = set()           # 入り直しの最中のサーバー
        self._voice_lost: set[int] = set()          # 再接続の結果を待っているサーバー

    async def setup_hook(self):
        for f in TEMP_DIR.iterdir():   # 前回の消し残し（音声と、A.I.VOICE が一緒に作る .txt）を掃除
            if f.is_file() and f.suffix.lower() in (".wav", ".txt"):
                _remove_quietly(str(f))

        for key in ENABLED_ENGINES:
            eng = ENGINE_CLASSES[key]()
            try:
                await eng.start()
                engines[key] = eng
            except Exception as e:
                log.error("%s 初期化失敗: %s", ENGINE_LABELS[key], e)
                eng.executor.shutdown(wait=False)
                # 自動再接続の最初の試行は、少し待ってから（起動した直後に何度も試さない）
                self.retry_state[key] = {
                    "next": time.monotonic() + RECONNECT_INTERVAL_SECONDS,
                    "interval": RECONNECT_INTERVAL_SECONDS,
                }

        if not engines:
            if not ENGINE_AUTO_RECONNECT:
                raise RuntimeError("使用可能なエンジンがありません。終了します。")
            log.warning("使用できるエンジンがありません。エンジンが起動するのを待って、自動で再接続します")
        rebuild_unified_voices()
        migrate_legacy_settings()
        self.add_view(SettingsLauncher())   # 置いてある「設定を開く」メッセージのボタンを、再起動後も使えるように
        log.info("エンジン: %s", list(engines.keys()))

        try:
            synced = await self.tree.sync()
            log.info("コマンド同期: %d", len(synced))
        except Exception as e:
            log.error("同期エラー: %s", e)

        self._tasks.append(asyncio.create_task(self._presence_loop()))
        if ENGINE_AUTO_RECONNECT:
            self._tasks.append(asyncio.create_task(self._engine_watcher()))

    # ---- ステータス ----
    async def apply_presence(self, force: bool = False):
        status, kind, text = effective_presence()
        if not force and (status, kind, text) == self._last_presence:
            return
        await self.change_presence(status=STATUS_MAP[status], activity=build_activity(kind, text))
        self._last_presence = (status, kind, text)

    async def _presence_loop(self):
        while True:
            await self._presence_event.wait()
            await asyncio.sleep(5)   # 続けて接続・切断されたときに、まとめて1回だけ反映する
            self._presence_event.clear()
            try:
                await self.apply_presence()
            except Exception:
                log.exception("ステータスの更新に失敗")

    # ---- エンジンの自動再接続 ----
    async def _engine_watcher(self):
        """起動に失敗したエンジン・連続で失敗しているエンジンに、間隔をあけながら再接続を試みる"""
        while True:
            await asyncio.sleep(10)
            changed = False
            for key in ENABLED_ENGINES:
                st = self.retry_state.setdefault(
                    key, {"next": 0.0, "interval": RECONNECT_INTERVAL_SECONDS}
                )
                eng = engines.get(key)
                healthy = eng is not None and not (eng.can_reconnect and eng.fail_streak >= FAIL_NOTICE_THRESHOLD)
                if healthy:
                    # 正常。次に問題が起きたとき、すぐ再接続を試せるよう元に戻しておく
                    st["next"], st["interval"] = 0.0, RECONNECT_INTERVAL_SECONDS
                    continue
                now = time.monotonic()
                if now < st["next"]:
                    continue
                try:
                    if eng is None:
                        new = ENGINE_CLASSES[key]()
                        try:
                            await new.start(RECONNECT_TIMEOUT_SECONDS)
                        except Exception:
                            new.executor.shutdown(wait=False)
                            raise
                        engines[key] = new
                        log.info("%s に接続できました（自動再接続）", ENGINE_LABELS[key])
                    else:
                        await eng.reconnect()
                        log.info("%s を再接続しました", ENGINE_LABELS[key])
                    changed = True
                    st["next"], st["interval"] = 0.0, RECONNECT_INTERVAL_SECONDS
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    st["next"] = now + st["interval"]
                    log.warning("%s への再接続に失敗（次は%d秒後）: %s", ENGINE_LABELS[key], st["interval"], e)
                    st["interval"] = min(st["interval"] * 2, 300)
            if changed:
                rebuild_unified_voices()
                migrate_legacy_settings()

    # ---- 再起動後の自動復帰 ----
    async def restore_sessions(self):
        for gid, s in list(sessions_store.data.items()):
            try:
                guild = self.get_guild(int(gid))
                vc_ch = guild.get_channel(int(s.get("vc", 0))) if guild else None
                text_id = int(s.get("text", 0))
                usable = isinstance(vc_ch, (discord.VoiceChannel, discord.StageChannel)) \
                    and any(not m.bot for m in vc_ch.members)   # 誰もいないVCには戻らない
                if not usable:
                    sessions_store.data.pop(gid, None)
                    with contextlib.suppress(OSError):
                        sessions_store.save()
                    continue
                player = get_player(guild)
                if player.active:
                    continue
                await player.connect(vc_ch, text_id)
                log.info("読み上げを再開しました: guild=%s", gid)
                ch = guild.get_channel_or_thread(text_id)
                if ch is not None:
                    with contextlib.suppress(Exception):
                        await ch.send("読み上げを再開しました")
            except Exception:
                log.exception("自動復帰に失敗: guild=%s", gid)
                sessions_store.data.pop(gid, None)
                with contextlib.suppress(OSError):
                    sessions_store.save()
            await asyncio.sleep(1)

    # ---- 切断されたときの入り直し ----
    async def voice_lost(self, guild: discord.Guild, player: "GuildPlayer", vc: discord.VoiceClient):
        """
        discord.py は通信が切れると自分で入り直し、その途中でも「VCにいない」と通知が来る。
        ここで片付けると進行中の再接続が壊れ、次の接続も応答が来ずに失敗するので、結果が出てから判断する。
        discord.py があきらめたときや、外から切断されたときは guild.voice_client が消える。
        """
        if guild.id in self._voice_lost:
            return
        self._voice_lost.add(guild.id)
        try:
            for _ in range(VOICE_RECOVER_WAIT_SECONDS):
                await asyncio.sleep(1)
                if self._closing or player.voice_client is not vc:
                    return   # /leave など、別の操作で片付いた
                if vc.is_connected():
                    log.info("VCに再接続しました: guild=%s", guild.id)
                    return
                if guild.voice_client is not vc:
                    break
            vc_id, text_id = player.voice_channel_id, player.text_channel_id
            await player.shutdown()   # 状態を片付ける
            if AUTO_REJOIN and vc_id is not None and text_id is not None:
                log.warning("VCから切断されました: guild=%s → 入り直します", guild.id)
                spawn(self.rejoin(guild, vc_id, text_id))
        finally:
            self._voice_lost.discard(guild.id)

    async def rejoin(self, guild: discord.Guild, vc_id: int, text_id: int):
        if guild.id in self._rejoining:
            return
        self._rejoining.add(guild.id)
        try:
            for wait in REJOIN_DELAYS:
                await asyncio.sleep(wait)
                if self._closing:
                    return
                player = players.get(guild.id)
                if player is not None and player.active:
                    return   # すでに戻っている（/join された、など）
                vc_ch = guild.get_channel(vc_id)
                if not isinstance(vc_ch, (discord.VoiceChannel, discord.StageChannel)) \
                        or not any(not m.bot for m in vc_ch.members):
                    return   # VCが無い・無人なら戻らない
                try:
                    await get_player(guild).connect(vc_ch, text_id)
                except Exception as e:
                    log.warning("VCへの入り直しに失敗: guild=%s (%s)", guild.id, e)
                    continue
                log.info("切断されたため、VCに入り直しました: guild=%s", guild.id)
                ch = guild.get_channel_or_thread(text_id)
                if ch is not None:
                    with contextlib.suppress(Exception):
                        await ch.send("接続が切れたため、読み上げを再開しました")
                return
            log.error("VCへの入り直しをあきらめました: guild=%s", guild.id)
            ch = guild.get_channel_or_thread(text_id)
            if ch is not None:
                with contextlib.suppress(Exception):
                    await ch.send("⚠ VCとの接続が切れて、戻れませんでした。/join でもう一度接続してください")
        finally:
            self._rejoining.discard(guild.id)

    async def close(self):
        if not self._cleaned_up:
            self._cleaned_up = True
            self._closing = True
            for t in self._tasks:
                t.cancel()
            for p in list(players.values()):
                with contextlib.suppress(Exception):
                    await p.shutdown(forget=False)   # 自動復帰のため、接続していたVCの記録は残す
            for e in list(engines.values()):
                await e.stop()
        await super().close()


bot = TTSBot()


@bot.event
async def on_ready():
    log.info("ログイン完了: %s", bot.user)
    log.info("参加しているサーバー: %d", len(bot.guilds))
    for g in bot.guilds:
        mark = "" if not ALLOWED_GUILDS or g.id in ALLOWED_GUILDS else "  ← 許可リストにないので抜けます"
        log.info("  ・%s（id=%s、%s人）%s", g.name, g.id, g.member_count, mark)
    for g in list(bot.guilds):
        await leave_if_not_allowed(g)
    if not bot._restored:   # on_ready は再接続のたびに呼ばれるので、復帰は1回だけ
        bot._restored = True
        if AUTO_RESUME and sessions_store.data:
            spawn(bot.restore_sessions())


async def _send_leave_message(guild: discord.Guild):
    """抜ける前のひとことを、送れるチャンネルに1回だけ送る（システムチャンネル → 送れる最初のテキストチャンネル）"""
    me = guild.me
    candidates = []
    if guild.system_channel is not None:
        candidates.append(guild.system_channel)
    candidates += sorted(guild.text_channels, key=lambda c: c.position)
    for ch in candidates:
        try:
            if me is not None and not ch.permissions_for(me).send_messages:
                continue
            await ch.send(LEAVE_MESSAGE)
            log.info("退出のお知らせを送りました: %s #%s", guild.name, ch.name)
            return
        except Exception:
            continue
    log.info("退出のお知らせを送れるチャンネルがありませんでした: %s", guild.name)


async def leave_if_not_allowed(guild: discord.Guild) -> bool:
    """許可リスト（allowed_guilds）にないサーバーから抜ける。抜けたら True"""
    if not ALLOWED_GUILDS or guild.id in ALLOWED_GUILDS:
        return False
    player = players.pop(guild.id, None)
    if player is not None:
        with contextlib.suppress(Exception):
            await player.shutdown()
    if LEAVE_MESSAGE:
        await _send_leave_message(guild)
    try:
        await guild.leave()
        log.warning("許可リストにないサーバーから抜けました: %s（id=%s）", guild.name, guild.id)
        return True
    except Exception:
        log.exception("サーバーから抜けられませんでした: %s（id=%s）", guild.name, guild.id)
        return False


@bot.event
async def on_guild_join(guild: discord.Guild):
    log.warning("サーバーに追加されました: %s（id=%s、%s人）", guild.name, guild.id, guild.member_count)
    await leave_if_not_allowed(guild)


@bot.event
async def on_guild_remove(guild: discord.Guild):
    log.warning("サーバーから外れました: %s（id=%s）", guild.name, guild.id)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    """読み上げ待ち・再生中の発言が削除されたら、読み上げを取り消す"""
    if payload.guild_id is None:
        return
    player = players.get(payload.guild_id)
    if player is not None:
        player.cancel_message(payload.message_id)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    player = players.get(message.guild.id)
    if player is None or not player.active or message.channel.id != player.text_channel_id:
        return
    if not engines:   # どのエンジンも使えない間は読めない
        return
    # BOTと同じVCにいる人の発言だけ読む
    voice = getattr(message.author, "voice", None)
    if voice is None or voice.channel != player.voice_client.channel:
        return

    text = build_speech_text(message)
    if not text:
        return

    gs = guild_settings.get(message.guild.id)
    if gs["read_name"]:
        now = time.monotonic()
        last = player.last_speaker
        if last is None or last[0] != message.author.id or now - last[1] > NAME_REPEAT_SECONDS:
            name = dictionary.apply(message.guild.id, strip_emoji(message.author.display_name)).strip()
            if name:
                text = f"{name}、{text}"
        player.last_speaker = (message.author.id, now)

    if player.enqueue(make_utterance(text, message.author.id, message.id)) and LOG_READS:
        if LOG_READ_TEXT:
            log.info("読み上げ: [%s] #%s %s（%s）: %s", message.guild.name, message.channel.name,
                     message.author.display_name, message.author.id, text)
        else:
            log.info("読み上げ: [%s] #%s %s（%s）", message.guild.name, message.channel.name,
                     message.author.display_name, message.author.id)


@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    if bot._closing:   # BOT自身の終了中の切断は、後片付けしない（自動復帰の記録を残すため）
        return
    player = players.get(member.guild.id)
    if player is None or player.voice_client is None:
        return

    if member.id == bot.user.id:
        if after.channel is None:
            # /leave や無人での退出は、先に voice_client を外すので、ここには来ない。
            # ここに来るのは、通信の不具合や、手動で切断されたとき
            spawn(bot.voice_lost(member.guild, player, player.voice_client))
        elif player.voice_channel_id != after.channel.id:
            player.voice_channel_id = after.channel.id   # 別のVCへ移動させられた場合は、記録も更新
            player._save_session()
        return

    channel = player.voice_client.channel
    if channel is None:
        return
    if sum(1 for m in channel.members if not m.bot) == 0:
        await player.shutdown()
        return

    if not guild_settings.get(member.guild.id)["announce"] or not engines:
        return
    name = dictionary.apply(member.guild.id, strip_emoji(member.display_name)).strip() or "ユーザー"
    # 入退室のアナウンスは、その人の声で読む（他のBOTの入退室は既定の声）
    speaker = None if member.bot else member.id
    if after.channel == channel and before.channel != channel:
        player.enqueue(make_utterance(f"{name}さんが入室しました", speaker))
    elif before.channel == channel and after.channel != channel:
        player.enqueue(make_utterance(f"{name}さんが退室しました", speaker))


# ====================== コマンド ======================
@bot.tree.command(name="join", description="VCに接続して、このチャンネルの読み上げを開始")
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    vstate = interaction.user.voice
    if vstate is None or vstate.channel is None:
        await interaction.response.send_message("先にVCへ入ってください", ephemeral=True)
        return
    player = get_player(interaction.guild)
    if player.active:
        if player.voice_client.channel == vstate.channel:
            player.set_text_channel(interaction.channel_id)
            await interaction.response.send_message("読み上げるチャンネルをここに変更しました")
        else:
            await interaction.response.send_message("別のVCで使用中です", ephemeral=True)
        return
    if player.voice_client is not None:   # ここで接続し直すと、進行中の再接続が壊れる
        await interaction.response.send_message("VCに再接続しています。少し待ってください", ephemeral=True)
        return

    await interaction.response.defer()   # 接続に3秒以上かかっても失敗しないように
    try:
        await player.connect(vstate.channel, interaction.channel_id)
    except Exception:
        log.exception("VC接続に失敗")
        await interaction.followup.send("接続に失敗しました", ephemeral=True)
        return
    log.info("/join: [%s] VC=%s 読み上げ=#%s 実行=%s（%s）", interaction.guild.name, vstate.channel.name,
             getattr(interaction.channel, "name", interaction.channel_id),
             interaction.user.display_name, interaction.user.id)
    await interaction.followup.send(f"**{vstate.channel.name}** に接続しました。このチャンネルの発言を読み上げます")


@bot.tree.command(name="leave", description="VCから切断")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    player = players.get(interaction.guild.id)
    if player is None or not player.active:
        await interaction.response.send_message("未接続です", ephemeral=True)
        return
    await player.shutdown()
    await interaction.response.send_message("切断しました")


@bot.tree.command(name="skip", description="いまの読み上げをスキップ")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    player = players.get(interaction.guild.id)
    if player:
        player.skip()
    await interaction.response.send_message("スキップ", ephemeral=True)


@bot.tree.command(name="clear", description="読み上げ待ちを全部消す")
@app_commands.guild_only()
async def clear_cmd(interaction: discord.Interaction):
    player = players.get(interaction.guild.id)
    if player:
        player.clear()
    await interaction.response.send_message("クリアしました")


@bot.tree.command(name="settings", description="設定画面（声・速度・試し聞き・辞書・サーバー設定）")
@app_commands.describe(tab="最初に開く画面")
@app_commands.choices(tab=[
    app_commands.Choice(name="自分の声", value="me"),
    app_commands.Choice(name="辞書", value="dict"),
    app_commands.Choice(name="サーバー設定", value="server"),
    app_commands.Choice(name="BOT（表示・再起動。BOTオーナーのみ）", value="bot"),
])
async def settings_cmd(interaction: discord.Interaction, tab: Optional[str] = None):
    await open_settings(interaction, tab or "me")


@bot.tree.command(name="status", description="BOTの状態（エンジン・接続数）")
async def status_cmd(interaction: discord.Interaction):
    lines = ["**エンジン**"]
    for key in ENABLED_ENGINES:
        eng = engines.get(key)
        retry = "（自動で再接続を試しています）" if ENGINE_AUTO_RECONNECT else ""
        if eng is None:
            lines.append(f"・{ENGINE_LABELS[key]}: 未接続{retry}")
        elif eng.hung:
            lines.append(f"・{eng.display_name}: 固まっています（エンジンを再起動すると直ります）")
        elif eng.fail_streak >= FAIL_NOTICE_THRESHOLD:
            lines.append(f"・{eng.display_name}: 不調（連続{eng.fail_streak}回失敗）{retry if eng.can_reconnect else ''}")
        else:
            lines.append(f"・{eng.display_name}: 稼働中（{len(eng.voices)}話者）")
    active = sum(1 for p in players.values() if p.active)
    lines.append(f"\n接続中のサーバー: {active}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


CREDIT_URL = cfg_str("bot", "credit_url")   # クレジットのページ（空なら出さない）
# VOICEVOX で、キャラクター名と別の表記が決められているもの（各キャラクターの利用規約より）
VOICEVOX_CREDIT_NAMES = {
    "もち子さん": "もち子(cv 明日葉よもぎ)",
    "Voidoll": "Voidoll(CV:丹下桜)",
    "ユーレイちゃん": "ユーレイちゃん(CV:神崎零)",
    "里石ユカ": "里石ユカ（つぼみ）",
}


def _character_name(label: str) -> str:
    """「ずんだもん（ノーマル）」→「ずんだもん」（話し方の違いを1つにまとめる）"""
    # 話し方の名前にも（）が入ることがあるので（例: 後鬼（人間（怒り）ver.））、最初の「（」で切る
    if label.endswith("）") and "（" in label and not label.startswith("（"):
        return label[:label.index("（")]
    return label


def credit_lines() -> list[str]:
    """いま動いているエンジンと話者から、クレジットの一覧を作る"""
    lines = []
    for key in ENGINE_ORDER:
        eng = engines.get(key)
        if eng is None or not eng.voices:
            continue
        names = list(dict.fromkeys(_character_name(v.label) for v in eng.voices))
        if key == "voicevox":
            body = "、".join(f"VOICEVOX:{VOICEVOX_CREDIT_NAMES.get(n, n)}" for n in names)
        else:
            body = "、".join(names)
        lines.append(f"**{eng.display_name}**\n{body}")
    return lines


@bot.tree.command(name="credit", description="使っている音声のクレジット（キャラクター名・利用規約）")
async def credit_cmd(interaction: discord.Interaction):
    parts = ["🎙 **この読み上げBOTで使っている音声**"]
    parts += credit_lines() or ["（いま動いている音声エンジンがありません）"]
    tail = "各キャラクターの利用規約に従って利用しています。"
    if CREDIT_URL:
        tail += f"\n表記のしかた・利用規約の一覧: {CREDIT_URL}"
    text = "\n\n".join(parts)
    if len(text) + len(tail) > 1950:
        text = text[:1950 - len(tail)] + "…"
    await interaction.response.send_message(f"{text}\n\n{tail}", ephemeral=True)


async def request_restart(user_id: int):
    """再起動する（BOTオーナーだけ。設定画面の 🤖 BOT タブから）"""
    global RESTART_REQUESTED
    log.info("[RESTART] %s", user_id)
    RESTART_REQUESTED = True
    await bot.close()   # エンジンの後始末は TTSBot.close が行う


def describe_presence() -> str:
    st, kd, tx = effective_presence()
    shown = f"{tx}（{ACTIVITY_LABELS[kd]}）" if tx else "なし"
    mode = "自動表示" if presence_auto() else "手動"
    return f"ステータス: **{STATUS_LABELS[st]}**\n表示: **{shown}**（{mode}）"


async def change_presence(status: Optional[str] = None, activity: Optional[str] = None,
                          kind: Optional[str] = None, auto: Optional[bool] = None) -> Optional[str]:
    """BOTのステータス・表示を変えて保存する（設定画面の 🤖 BOT タブから）。失敗したらエラー文を返す
    kind="none" で表示を消す。文字を指定すると自動表示はオフになる"""
    cur_status, cur_kind, cur_text = stored_presence()
    new_status = status or cur_status
    new_kind = kind if kind and kind != "none" else cur_kind
    new_text = cur_text
    new_auto = presence_auto() if auto is None else bool(auto)

    if kind == "none":
        new_text = None
        if auto is None:
            new_auto = False   # 「表示を消す」なら、自動表示も止める
    elif activity is not None:
        new_text = activity.strip()[:ACTIVITY_MAX_LEN] or None
        if auto is None:
            new_auto = False   # 文字を指定したなら、その文字を表示する
    elif kind is not None and not cur_text and not new_auto:
        return "先に、表示する文字を入力してください"

    old = dict(presence_store.data)
    presence_store.data.update({"status": new_status, "kind": new_kind, "text": new_text, "auto": new_auto})
    try:
        await bot.apply_presence(force=True)
    except Exception:
        log.exception("プレゼンス変更に失敗")
        presence_store.data.clear()
        presence_store.data.update(old)   # 反映できなかったので、保存する設定も元に戻す
        return "変更に失敗しました"
    presence_store.save()
    return None


@bot.tree.command(name="help_read", description="コマンド一覧")
async def help_read(interaction: discord.Interaction):
    lines = ["声・速度・辞書・サーバー設定・試し聞きは、すべて `/settings` からできます", "", "**コマンド**"]
    for c in sorted(bot.tree.get_commands(), key=lambda c: c.name):
        lines.append(f"`/{c.name}` … {c.description}")
    lines.append("\n`;` `//` `/` で始まる発言は読み上げません")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ====================== 起動 ======================
def load_token() -> str:
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token and TOKEN_FILE.exists():
        lines = TOKEN_FILE.read_text(encoding="utf-8-sig").strip().splitlines()
        token = lines[0].strip() if lines else ""
    # うっかり「TOKEN=xxxx」や "xxxx" と書いてしまっても読めるようにしておく
    if "=" in token:
        token = token.split("=", 1)[1]
    return token.strip().strip("\"'").strip()


def setup_logging():
    """画面に出すログを、ファイル（古いものは自動で整理）にも残す"""
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_error = None
    if LOG_TO_FILE:
        try:
            from logging.handlers import RotatingFileHandler
            log_dir = DATA_DIR / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            handlers.append(RotatingFileHandler(
                log_dir / "bot.log", maxBytes=LOG_MAX_MB * 1024 * 1024,
                backupCount=LOG_BACKUPS, encoding="utf-8",
            ))
        except OSError as e:
            log_error = e   # ログファイルを作れなくても、BOTは動かす
    logging.basicConfig(
        level=level, handlers=handlers, force=True,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if log_error is not None:
        log.warning("ログファイルを作れませんでした（画面にだけ出力します）: %s", log_error)


def exit_process(code: int = 0):
    """終了する。固まったスレッドが残っていると普通の終了では終われないので、そのときは強制的に終わる"""
    if TTSEngine.any_stuck:
        logging.shutdown()
        sys.stdout.flush()
        os._exit(code)
    sys.exit(code)


def main():
    setup_logging()
    log.info("設定ファイル: %s（%s）", CONFIG_PATH.name, "読み込み" if CONFIG_PATH.exists() else "なし → 既定値")
    token = load_token()
    if not token:
        log.error("トークンがありません。%s にBOTトークンを1行で書いてください。", TOKEN_FILE)
        print(f"トークンがありません。{TOKEN_FILE} にBOTトークンを1行で書いてください。")
        sys.exit(1)

    try:
        bot.run(token, log_handler=None)
    except Exception:
        log.exception("致命的なエラーで終了しました")   # 画面を閉じたあとでも、原因をログで追えるように
        raise

    if RESTART_REQUESTED:
        if os.environ.get("MULTITTSBOT_LOOP") == "1":
            # Start.bat のくり返し起動の中で動いている → 終了コード 3 で抜け、Start.bat にすぐ起動し直してもらう
            # （ここで新しいプロセスも起動すると、BOTが2つ動いてしまう）
            exit_process(3)
        # os.execv はWindowsで挙動が不安定なので、新プロセスを起動してから終了する
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        subprocess.Popen([sys.executable, *sys.argv], creationflags=flags)
    if TTSEngine.any_stuck:
        exit_process(0)


if __name__ == "__main__":
    main()
