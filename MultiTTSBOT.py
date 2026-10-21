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

OWNER_IDS = {
    int(x) for x in cfg_list("bot", "owner_ids", "266447175695728640, 1193926853145145384")
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

# --- [engine] ---
VOICEROID_INSTALL_PATH = cfg_str("engine", "voiceroid_install_path") or None
VOICEROID_INSTALL_PATH_X86 = cfg_str("engine", "voiceroid_install_path_x86") or None
AIVOICE_EDITOR_DIR = cfg_str("engine", "aivoice_editor_dir") or None
VOICEVOX_HOST = cfg_str("engine", "voicevox_host", "http://127.0.0.1:50021")
# 同じエンジンを複数のBOTで共有するとき、合成中は他のBOTを待たせる（VOICEROID2 / A.I.VOICE / CeVIO）
ENGINE_SHARED_LOCK = cfg_bool("engine", "shared_lock", True)
LOCK_TIMEOUT_SECONDS = cfg_int("engine", "lock_timeout_seconds", 120)
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
# 未設定のサーバーでの初期値（サーバーごとに /readname /announce で変更できる）
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

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tts-{self.name}")
        self.voices: list[Voice] = []
        self._by_key: dict[str, Voice] = {}
        self.fail_streak = 0                    # 連続で読み上げに失敗した回数
        self.last_error: Optional[str] = None   # 直近のエラー内容（/status で表示）
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

    async def start(self):
        await self._call(self._run_locked, self._setup)
        self._by_key = {v.key: v for v in self.voices}
        self.fail_streak = 0
        self.last_error = None

    async def reconnect(self):
        """エンジンを初期化し直す（落ちていたエンジンが復活したときの再接続用）"""
        await self.start()

    async def synth(self, text: str, voice_key: str, speed: float, pitch: float) -> Optional[str]:
        """話者切替→合成を1つのまとまりとして専用スレッドで実行し、wavのパスを返す"""
        try:
            path = await self._call(self._run_locked, self._synth, text, voice_key, speed, pitch)
        except Exception as e:
            self.fail_streak += 1
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            raise
        self.fail_streak = 0   # 成功したら数え直し（読み飛ばしの None も、エンジンは正常）
        return path

    async def stop(self):
        with contextlib.suppress(Exception):
            await self._call(self._teardown)
        self.executor.shutdown(wait=False)

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

    def __init__(self, filename: str):
        self.path = DATA_DIR / filename
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

    def save(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


class UserSettings(JsonStore):
    # auto: 未設定だったので自動で割り当てた話者（/voice で選ぶと False になる）
    DEFAULTS = {"engine": None, "voice": None, "auto": False, "speed": 1.0, "pitch": 1.0}

    def get(self, user_id: int) -> dict:
        s = self.data.setdefault(str(user_id), {})
        for k, v in self.DEFAULTS.items():
            s.setdefault(k, v)
        return s

    def update(self, user_id: int, **kwargs):
        s = self.get(user_id)
        s.update(kwargs)
        if "voice" in kwargs:   # 旧形式（番号保存）の値は捨てる
            s.pop("voice_index", None)
            s.pop("unified_index", None)
        self.save()


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


class Dictionary(JsonStore):
    def __init__(self, filename: str):
        super().__init__(filename)
        self._cache: dict[str, tuple] = {}

    def get_map(self, guild_id: int) -> dict[str, str]:
        return self.data.get(str(guild_id), {})

    def add(self, guild_id: int, word: str, reading: str):
        self.data.setdefault(str(guild_id), {})[word] = reading
        self._cache.pop(str(guild_id), None)
        self.save()

    def remove(self, guild_id: int, word: str) -> bool:
        m = self.data.get(str(guild_id), {})
        if word in m:
            del m[word]
            self._cache.pop(str(guild_id), None)
            self.save()
            return True
        return False

    def _compiled(self, guild_id: int):
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
settings = UserSettings(SETTINGS_FILE)
guild_settings = GuildSettings(GUILD_SETTINGS_FILE)
dictionary = Dictionary(DICT_FILE)
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
    /presence で一度も指定していない場合は config の初期値。ただし以前に自分で文字を設定していたなら、それを消さない"""
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
        settings.update(user_id, engine=eng.name, voice=v.key, auto=True)
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
    targets = []
    for uid, s in settings.data.items():
        if not isinstance(s, dict) or s.get("voice") is not None:
            continue
        if "voice_index" not in s:
            continue
        eng = engines.get(s.get("engine") or "roid")
        if eng is None:
            continue
        v = _legacy_voice(eng, s.get("voice_index"))
        if v is not None:
            targets.append((uid, s, eng, v))
        elif uid not in _migration_warned:
            # 番号が範囲外など。推測で割り当てず、そのまま残す
            _migration_warned.add(uid)
            log.warning("移行できません（番号が範囲外）: user=%s engine=%s voice_index=%r → そのまま残します",
                        uid, eng.name, s.get("voice_index"))
    if not targets:
        return 0
    backup = settings.path.with_name(settings.path.name + ".bak-before-migration")
    if settings.path.exists() and not backup.exists():
        with contextlib.suppress(OSError):
            shutil.copy2(settings.path, backup)
            log.info("移行前の設定を %s に保存しました", backup.name)
    for uid, s, eng, v in targets:
        s["engine"], s["voice"], s["auto"] = eng.name, v.key, False
        s.pop("voice_index", None)
        s.pop("unified_index", None)
    try:
        settings.save()
    except OSError:
        log.exception("設定の移行結果の保存に失敗")
        return 0
    log.info("旧形式の話者設定を %d人分、話者名の形式に移行しました", len(targets))
    return len(targets)


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


# ====================== 話者一覧UI ======================
def find_unified_index(user_id: int) -> Optional[int]:
    try:
        eng, voice = resolve_user_voice(user_id)
    except RuntimeError:
        return None
    return unified_index_by_key.get((eng.name, voice.key))


class VoicesPager(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.filter_engine: Optional[str] = None
        self.page = 0

        options = [discord.SelectOption(label="すべて", value="__all__")]
        options += [
            discord.SelectOption(label=e.display_name, value=e.name) for e in engines.values()
        ]
        self.select = discord.ui.Select(placeholder="エンジンで絞り込み", options=options, row=0)
        self.select.callback = self._on_select
        self.add_item(self.select)
        self._refresh()

    def _items(self) -> list[tuple[int, UVoice]]:
        return [
            (i, u) for i, u in enumerate(unified)
            if self.filter_engine is None or u.engine.name == self.filter_engine
        ]

    def _refresh(self):
        self.items = self._items()
        self.total_pages = max(1, (len(self.items) + VOICES_PER_PAGE - 1) // VOICES_PER_PAGE)
        self.page = max(0, min(self.page, self.total_pages - 1))
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def build_content(self) -> str:
        self._refresh()
        filt = "すべて" if self.filter_engine is None else ENGINE_LABELS.get(
            self.filter_engine, self.filter_engine
        )
        current = find_unified_index(self.user_id)
        start = self.page * VOICES_PER_PAGE
        end = min(start + VOICES_PER_PAGE, len(self.items))
        lines = [
            f"**話者一覧**｜フィルタ: `{filt}`｜{self.page + 1}/{self.total_pages}ページ",
            f"該当 {len(self.items)} 件（全体 {len(unified)}）",
            "`/voice` に名前（一部でOK）か番号を入れて選択",
            "",
        ]
        for i, u in self.items[start:end]:
            mark = " ← あなた" if i == current else ""
            lines.append(f"`{i}` : {u.label}{mark}")
        if not self.items:
            lines.append("（該当なし）")
        return "\n".join(lines)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("他の人の一覧です", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        value = self.select.values[0]
        self.filter_engine = None if value == "__all__" else value
        self.page = 0
        await interaction.response.edit_message(content=self.build_content(), view=self)

    @discord.ui.button(label="◀ 前へ", style=discord.ButtonStyle.primary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await interaction.response.edit_message(content=self.build_content(), view=self)

    @discord.ui.button(label="次へ ▶", style=discord.ButtonStyle.primary, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.total_pages - 1, self.page + 1)
        await interaction.response.edit_message(content=self.build_content(), view=self)


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

    async def setup_hook(self):
        for f in TEMP_DIR.glob("*.wav"):   # 前回の消し残しを掃除
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
                            await new.start()
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
    if not bot._restored:   # on_ready は再接続のたびに呼ばれるので、復帰は1回だけ
        bot._restored = True
        if AUTO_RESUME and sessions_store.data:
            spawn(bot.restore_sessions())


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

    player.enqueue(make_utterance(text, message.author.id, message.id))


@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    if bot._closing:   # BOT自身の終了中の切断は、後片付けしない（自動復帰の記録を残すため）
        return
    player = players.get(member.guild.id)
    if player is None or player.voice_client is None:
        return

    if member.id == bot.user.id:
        if after.channel is None:
            await player.shutdown()   # 強制切断された場合は状態を片付ける
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
    if after.channel == channel and before.channel != channel:
        player.enqueue(make_utterance(f"{name}さんが入室しました", None))
    elif before.channel == channel and after.channel != channel:
        player.enqueue(make_utterance(f"{name}さんが退室しました", None))


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

    await interaction.response.defer()   # 接続に3秒以上かかっても失敗しないように
    try:
        await player.connect(vstate.channel, interaction.channel_id)
    except Exception:
        log.exception("VC接続に失敗")
        await interaction.followup.send("接続に失敗しました", ephemeral=True)
        return
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


@bot.tree.command(name="voices", description="話者一覧（エンジンで絞り込み・ページ送り）")
async def voices(interaction: discord.Interaction):
    if not unified:
        await interaction.response.send_message("話者がありません", ephemeral=True)
        return
    view = VoicesPager(user_id=interaction.user.id)
    await interaction.response.send_message(content=view.build_content(), view=view, ephemeral=True)


async def voice_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.strip().lower()
    out = []
    for i, u in enumerate(unified):
        if cur in u.label.lower():
            out.append(app_commands.Choice(name=u.label[:100], value=str(i)))
            if len(out) >= 25:
                break
    return out


def find_voice_by_input(query: str) -> tuple[Optional[UVoice], list[UVoice]]:
    """番号 or 名前の一部から話者を探す。戻り値: (確定した話者, 複数ヒット時の候補)"""
    q = query.strip()
    if q.isdigit():
        i = int(q)
        return (unified[i], []) if 0 <= i < len(unified) else (None, [])
    ql = q.lower()
    exact = [u for u in unified if u.label.lower() == ql or u.voice.label.lower() == ql]
    if len(exact) == 1:
        return exact[0], []
    hits = [u for u in unified if ql in u.label.lower()]
    if len(hits) == 1:
        return hits[0], []
    return None, hits


@bot.tree.command(name="voice", description="話者を変更（名前で検索）。省略すると現在の設定を表示")
@app_commands.describe(voice="話者名（入力すると候補が出ます）")
@app_commands.autocomplete(voice=voice_autocomplete)
async def voice_cmd(interaction: discord.Interaction, voice: Optional[str] = None):
    uid = interaction.user.id
    if not engines:
        await interaction.response.send_message(
            "いま使える音声エンジンがありません。エンジンが起動すると、自動で接続します", ephemeral=True
        )
        return
    if voice is None:
        eng, v = resolve_user_voice(uid)
        s = settings.get(uid)
        auto = "（自動で割り当てた声です。`/voice` で変更できます）" if s.get("auto") else ""
        await interaction.response.send_message(
            f"**現在の設定**\n話者: `[{eng.display_name}] {v.label}`{auto}\n"
            f"速度: `{s['speed']}`　ピッチ: `{s['pitch']}`",
            ephemeral=True,
        )
        return

    found, hits = find_voice_by_input(voice)
    if found is None:
        if hits:
            names = "\n".join(f"・{u.label}" for u in hits[:8])
            more = f"\n…ほか{len(hits) - 8}件" if len(hits) > 8 else ""
            msg = f"候補が複数あります。もう少し絞ってください。\n{names}{more}"
        else:
            msg = "見つかりません。`/voices` で一覧を確認してください"
        await interaction.response.send_message(msg, ephemeral=True)
        return

    settings.update(uid, engine=found.engine.name, voice=found.voice.key, auto=False)
    await interaction.response.send_message(f"話者を変更 → **{found.label}**")


@bot.tree.command(name="tune", description="読み上げの速度・ピッチを変更（1.0が標準）。省略すると現在値を表示")
@app_commands.describe(speed="速度（0.5〜2.0）", pitch="ピッチ（0.5〜2.0）")
async def tune(
    interaction: discord.Interaction,
    speed: Optional[app_commands.Range[float, SPEED_MIN, SPEED_MAX]] = None,
    pitch: Optional[app_commands.Range[float, PITCH_MIN, PITCH_MAX]] = None,
):
    uid = interaction.user.id
    if speed is None and pitch is None:
        s = settings.get(uid)
        await interaction.response.send_message(
            f"速度 `{s['speed']}`　ピッチ `{s['pitch']}`", ephemeral=True
        )
        return
    changes = {}
    if speed is not None:
        changes["speed"] = float(speed)
    if pitch is not None:
        changes["pitch"] = float(pitch)
    settings.update(uid, **changes)
    s = settings.get(uid)
    await interaction.response.send_message(f"速度 **{s['speed']}**　ピッチ **{s['pitch']}**")


@bot.tree.command(name="test", description="いまの自分の声で、テスト読み上げをする")
@app_commands.guild_only()
async def test_cmd(interaction: discord.Interaction):
    player = players.get(interaction.guild.id)
    if player is None or not player.active:
        await interaction.response.send_message("先に /join で接続してください", ephemeral=True)
        return
    if not engines:
        await interaction.response.send_message("いま使える音声エンジンがありません", ephemeral=True)
        return
    player.enqueue(make_utterance("テストです。この声で読み上げます", interaction.user.id))
    await interaction.response.send_message("テスト読み上げを送りました", ephemeral=True)


@bot.tree.command(name="volume", description="読み上げ音量（このサーバー共通・管理者向け）")
@app_commands.describe(percent="10〜200（100が標準）")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def volume_cmd(interaction: discord.Interaction, percent: app_commands.Range[int, 10, 200]):
    guild_settings.update(interaction.guild.id, volume=int(percent))
    await interaction.response.send_message(f"音量 **{percent}%**")


@bot.tree.command(name="readname", description="発言者の名前も読み上げる（このサーバー共通・管理者向け）")
@app_commands.describe(on="ONにすると「〇〇、本文」と読みます")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def readname_cmd(interaction: discord.Interaction, on: bool):
    guild_settings.update(interaction.guild.id, read_name=bool(on))
    await interaction.response.send_message(f"名前の読み上げ: **{'ON' if on else 'OFF'}**")


@bot.tree.command(name="announce", description="VCの入退室を読み上げる（このサーバー共通・管理者向け）")
@app_commands.describe(on="ONにすると入退室を読み上げます")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def announce_cmd(interaction: discord.Interaction, on: bool):
    guild_settings.update(interaction.guild.id, announce=bool(on))
    await interaction.response.send_message(f"入退室の読み上げ: **{'ON' if on else 'OFF'}**")


@bot.tree.command(name="dict_add", description="辞書登録（サーバー共通）")
@app_commands.describe(word="表記", reading="読み")
@app_commands.guild_only()
async def dict_add(interaction: discord.Interaction, word: str, reading: str):
    word, reading = re.sub(r"\s+", " ", word.strip()), reading.strip()
    if not word or not reading:
        await interaction.response.send_message("表記と読みを入力してください", ephemeral=True)
        return
    problem = validate_dict_word(word)
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    if len(word) > DICT_MAX_WORD or len(reading) > DICT_MAX_READING:
        await interaction.response.send_message(
            f"長すぎます（表記{DICT_MAX_WORD}文字・読み{DICT_MAX_READING}文字まで）", ephemeral=True
        )
        return
    m = dictionary.get_map(interaction.guild.id)
    if word not in m and len(m) >= DICT_MAX_ENTRIES:
        await interaction.response.send_message(f"登録数が上限（{DICT_MAX_ENTRIES}件）です", ephemeral=True)
        return
    dictionary.add(interaction.guild.id, word, reading)
    await interaction.response.send_message(f"登録: `{word}` → `{reading}`")


@bot.tree.command(name="dict_remove", description="辞書削除")
@app_commands.describe(word="表記")
@app_commands.guild_only()
async def dict_remove(interaction: discord.Interaction, word: str):
    if dictionary.remove(interaction.guild.id, word.strip()):
        await interaction.response.send_message(f"削除: `{word}`")
    else:
        await interaction.response.send_message("見つかりません", ephemeral=True)


@bot.tree.command(name="dict_list", description="辞書一覧")
@app_commands.guild_only()
async def dict_list(interaction: discord.Interaction):
    m = dictionary.get_map(interaction.guild.id)
    if not m:
        await interaction.response.send_message("辞書は空です", ephemeral=True)
        return
    text = "**辞書**\n" + "\n".join(
        f"`{w}` → `{r}`" + ("　⚠使われません（登録できない語です）" if validate_dict_word(w) else "")
        for w, r in sorted(m.items())
    )
    if len(text) > 1900:
        text = text[:1900] + "\n…"
    await interaction.response.send_message(text, ephemeral=True)


@bot.tree.command(name="status", description="BOTの状態（エンジン・接続数）")
async def status_cmd(interaction: discord.Interaction):
    lines = ["**エンジン**"]
    for key in ENABLED_ENGINES:
        eng = engines.get(key)
        retry = "（自動で再接続を試しています）" if ENGINE_AUTO_RECONNECT else ""
        if eng is None:
            lines.append(f"・{ENGINE_LABELS[key]}: 未接続{retry}")
        elif eng.fail_streak >= FAIL_NOTICE_THRESHOLD:
            lines.append(f"・{eng.display_name}: 不調（連続{eng.fail_streak}回失敗）{retry if eng.can_reconnect else ''}")
        else:
            lines.append(f"・{eng.display_name}: 稼働中（{len(eng.voices)}話者）")
    active = sum(1 for p in players.values() if p.active)
    lines.append(f"\n接続中のサーバー: {active}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="credit", description="利用エンジンのクレジット・規約に関する案内")
async def credit_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        "各音声エンジン・キャラクターには利用規約があります。\n"
        "特にVOICEVOXは、音声を公開・配信する際に「VOICEVOX:キャラクター名」のクレジット表記が必要です。\n"
        "公開サーバーで使う場合は、各エンジンの規約を確認してください。",
        ephemeral=True,
    )


OWNER_ONLY_COMMANDS = {"restart", "presence"}


@bot.tree.command(name="presence", description="BOTのステータスと「プレイ中」表示を変更（管理者のみ）")
@app_commands.describe(
    status="オンライン状態",
    activity="表示する文字（例: /join で読み上げ）。指定すると自動表示はオフになります",
    kind="表示の種類（「表示を消す」でプレイ中を消去）",
    auto="自動表示（「〇サーバーで読み上げ中」）のオン/オフ",
)
@app_commands.choices(
    status=[app_commands.Choice(name=label, value=key) for key, label in STATUS_LABELS.items()],
    kind=[app_commands.Choice(name=f"{label}（{key}）", value=key) for key, label in ACTIVITY_LABELS.items()]
    + [app_commands.Choice(name="表示を消す", value="none")],
)
async def presence_cmd(
    interaction: discord.Interaction,
    status: Optional[str] = None,
    activity: Optional[str] = None,
    kind: Optional[str] = None,
    auto: Optional[bool] = None,
):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return

    def describe() -> str:
        st, kd, tx = effective_presence()
        shown = f"{tx}（{ACTIVITY_LABELS[kd]}）" if tx else "なし"
        mode = "自動表示" if presence_auto() else "手動"
        return f"ステータス: **{STATUS_LABELS[st]}**\n表示: **{shown}**（{mode}）"

    if status is None and activity is None and kind is None and auto is None:
        await interaction.response.send_message("**現在の設定**\n" + describe(), ephemeral=True)
        return

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
        await interaction.response.send_message("表示する文字を `activity` に入力してください", ephemeral=True)
        return

    old = dict(presence_store.data)
    presence_store.data.update({"status": new_status, "kind": new_kind, "text": new_text, "auto": new_auto})
    try:
        await bot.apply_presence(force=True)
    except Exception:
        log.exception("プレゼンス変更に失敗")
        presence_store.data.clear()
        presence_store.data.update(old)   # 反映できなかったので、保存する設定も元に戻す
        await interaction.response.send_message("変更に失敗しました", ephemeral=True)
        return

    presence_store.save()
    await interaction.response.send_message("変更しました\n" + describe(), ephemeral=True)


@bot.tree.command(name="restart", description="再起動（管理者のみ）")
async def restart(interaction: discord.Interaction):
    global RESTART_REQUESTED
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    await interaction.response.send_message("再起動します…")
    log.info("[RESTART] %s", interaction.user.id)
    RESTART_REQUESTED = True
    await bot.close()   # エンジンの後始末は TTSBot.close が行う


@bot.tree.command(name="help_read", description="コマンド一覧")
async def help_read(interaction: discord.Interaction):
    lines = ["**コマンド**"]
    for c in sorted(bot.tree.get_commands(), key=lambda c: c.name):
        if c.name in OWNER_ONLY_COMMANDS and not is_owner(interaction.user.id):
            continue
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
        # os.execv はWindowsで挙動が不安定なので、新プロセスを起動してから終了する
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        subprocess.Popen([sys.executable, *sys.argv], creationflags=flags)


if __name__ == "__main__":
    main()
