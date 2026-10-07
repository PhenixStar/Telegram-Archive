"""The viewer plays animated (.tgs) and video (.webm) stickers in the chat.

The helpers are EXECUTED under node, lifted verbatim out of the template: the
sticker kind (including video-sticker rows the backup files as 'video'), which
kinds a browser can play, the box size, the .tgs reader with its size caps, the
Lottie sanitizer, the text cache and the play budget. Template checks pin the
parts Vue renders. Demo data only.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

INDEX_HTML = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "index.html"
HTML = INDEX_HTML.read_text(encoding="utf-8")
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(not NODE, reason="node is required to run the sticker helpers")


def _setup_slice(declaration: str) -> str:
    """One top-level ``const`` of the root Vue ``setup()``, up to the next one.

    Setup-scope declarations are indented 16 spaces; nested ones are deeper and
    do not end the slice.
    """
    start = HTML.index(declaration)
    return HTML[start : HTML.index("\n                const ", start + len(declaration))]


def _run(declarations: tuple[str, ...], epilogue: str, prelude: str = "") -> Any:
    """Run the lifted declarations plus ``epilogue`` under node; return its one JSON line."""
    program = "\n".join([prelude, *(_setup_slice(d) for d in declarations), epilogue]) + "\n"
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "helpers.js"
        script.write_text(program, encoding="utf-8")
        result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr + "\n----\n" + program
    return json.loads(result.stdout)


_KIND = (
    "const getDocumentDisplayName = (msg) =>",
    "const VIDEO_STICKER_MAX_SIDE = ",
    "const VIDEO_STICKER_MAX_SECONDS = ",
    "const isVideoStickerRow = (media, name) =>",
    "const stickerKind = (msg) =>",
)


def _kinds(cases: dict[str, dict]) -> dict:
    program = f"const cases = {json.dumps(cases)};\n"
    program += "console.log(JSON.stringify(Object.fromEntries(Object.entries(cases).map(([k, m]) => [k, stickerKind({ media: m })]))))"
    return _run(_KIND, program)


@needs_node
class TestStickerKind:
    def test_sticker_rows_follow_the_mime_type_then_the_name(self):
        cases = {
            "tgsByMime": {
                "type": "sticker",
                "mime_type": "application/x-tgsticker",
                "file_name": "555_AnimatedSticker.tgs",
            },
            "tgsNullMime": {"type": "sticker", "mime_type": None, "file_name": "555_AnimatedSticker.tgs"},
            "tgsPathOnly": {"type": "sticker", "file_path": "/data/media/-1001/555_AnimatedSticker.tgs"},
            "webmByMime": {"type": "sticker", "mime_type": "video/webm", "file_name": "556_sticker.webm"},
            "webpByMime": {"type": "sticker", "mime_type": "image/webp", "file_name": "557_sticker.webp"},
            "webpNullMime": {"type": "sticker", "file_name": "557_sticker.webp"},
            "pngByMime": {"type": "sticker", "mime_type": "image/png", "file_name": "558_sticker.png"},
            # A video with stickers drawn on it that was filed as a sticker.
            "mp4": {"type": "sticker", "mime_type": "video/mp4", "file_name": "559_funny.mp4"},
            "noName": {"type": "sticker"},
            "photo": {"type": "photo", "file_name": "560.jpg"},
        }
        assert _kinds(cases) == {
            "tgsByMime": "tgs",
            "tgsNullMime": "tgs",
            "tgsPathOnly": "tgs",
            "webmByMime": "webm",
            "webpByMime": "image",
            "webpNullMime": "image",
            "pngByMime": "image",
            "mp4": "other",
            "noName": "other",
            "photo": None,
        }

    def test_video_rows_count_as_stickers_only_within_telegrams_limits(self):
        base = {"type": "video", "mime_type": "video/webm", "file_name": "561_sticker.webm"}
        cases = {
            "sticker": {**base, "width": 512, "height": 512, "duration": 3},
            "noDims": base,
            "pathOnly": {"type": "video", "file_path": "/data/media/-1001/561_sticker.webm"},
            "tooWide": {**base, "width": 1280, "height": 512},
            "tooLong": {**base, "width": 512, "height": 512, "duration": 9},
            "otherName": {**base, "file_name": "561_clip.webm"},
            "mp4Mime": {**base, "mime_type": "video/mp4"},
            "plainVideo": {"type": "video", "mime_type": "video/mp4", "file_name": "562_holiday.mp4"},
        }
        assert _kinds(cases) == {
            "sticker": "webm",
            "noDims": "webm",
            "pathOnly": "webm",
            "tooWide": None,
            "tooLong": None,
            "otherName": None,
            "mp4Mime": None,
            "plainVideo": None,
        }

    def test_a_kind_the_browser_cannot_play_keeps_the_label(self):
        program = (
            "const stickerSupport = { tgs: false, webm: false };\n"
            "const all = [\n"
            "  { media: { type: 'sticker', file_name: '1_AnimatedSticker.tgs' } },\n"
            "  { media: { type: 'video', mime_type: 'video/webm', file_name: '1_sticker.webm' } },\n"
            "  { media: { type: 'sticker', file_name: '1_sticker.webp' } },\n"
            "  { media: { type: 'sticker', mime_type: 'video/mp4', file_name: '1_funny.mp4' } },\n"
            "];\n"
            "const before = all.map(canPlaySticker);\n"
            "const labels = all.map(stickerLabel);\n"
            "stickerSupport.tgs = true; stickerSupport.webm = true;\n"
            "console.log(JSON.stringify({ before, after: all.map(canPlaySticker), labels }))"
        )
        result = _run((*_KIND, "const stickerLabel = (msg) =>", "const canPlaySticker = (msg) =>"), program)
        assert result == {
            "before": [False, False, True, False],
            "after": [True, True, True, False],
            "labels": ["Animated sticker", "Video sticker", "Sticker", "Sticker"],
        }


CHROME_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
SAFARI_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15"
CHROME_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/141.0 Mobile/15E148 Safari/604.1"
FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0"


def _playback(**env) -> dict:
    base = {"platform": "", "maxTouchPoints": 0, "decompression": True, "webm": True}
    return _run(
        ("const stickerPlayback = (env) =>",),
        f"console.log(JSON.stringify(stickerPlayback({json.dumps({**base, **env})})))",
    )


@needs_node
class TestStickerPlayback:
    @pytest.mark.parametrize("ua", [CHROME_MAC, FIREFOX_LINUX])
    def test_chrome_and_firefox_play_both_kinds(self, ua):
        assert _playback(userAgent=ua) == {"tgs": True, "webm": True}

    def test_safari_and_ios_keep_video_stickers_as_a_label(self):
        """No VP9 alpha there: a video sticker would sit on a black box."""
        assert _playback(userAgent=SAFARI_MAC) == {"tgs": True, "webm": False}
        assert _playback(userAgent=CHROME_IPHONE) == {"tgs": True, "webm": False}
        ipad = _playback(userAgent=SAFARI_MAC.replace("Version/26.0 ", ""), platform="MacIntel", maxTouchPoints=5)
        assert ipad == {"tgs": True, "webm": False}

    def test_missing_decoders_turn_the_kind_off(self):
        assert _playback(userAgent=CHROME_MAC, decompression=False) == {"tgs": False, "webm": True}
        assert _playback(userAgent=FIREFOX_LINUX, webm=False) == {"tgs": True, "webm": False}


@needs_node
def test_the_box_fits_the_file_into_200_by_192():
    program = (
        "console.log(JSON.stringify([\n"
        "  stickerBoxStyle({ media: { type: 'sticker' } }),\n"
        "  stickerBoxStyle({ media: { type: 'sticker', width: 512, height: 256 } }),\n"
        "  stickerBoxStyle({ media: { type: 'sticker', width: 300, height: 512 } }),\n"
        "]))"
    )
    decls = ("const STICKER_BOX_WIDTH = ", "const STICKER_BOX_HEIGHT = ", "const stickerBoxStyle = (msg) =>")
    assert _run(decls, program) == [
        {"width": "192px", "height": "192px"},
        {"width": "200px", "height": "100px"},
        {"width": "113px", "height": "192px"},
    ]


def _read_tgs(make_bytes: str) -> dict:
    program = (
        "const zlib = require('zlib'); const crypto = require('crypto');\n"
        "(async () => {\n"
        f"  const bytes = {make_bytes};\n"
        "  try {\n"
        "    const text = await readTgsText(new Response(bytes));\n"
        "    console.log(JSON.stringify({ ok: true, value: JSON.parse(text) }));\n"
        "  } catch (error) {\n"
        "    console.log(JSON.stringify({ ok: false, error: String(error.message || error) }));\n"
        "  }\n"
        "})()"
    )
    decls = ("const TGS_MAX_PACKED = ", "const TGS_MAX_UNPACKED = ", "const readTgsText = async (response) =>")
    return _run(decls, program)


@needs_node
class TestTgsReader:
    def test_a_gzipped_lottie_file_is_read(self):
        result = _read_tgs("zlib.gzipSync(Buffer.from(JSON.stringify({ v: '5.5.2', fr: 60, w: 512, h: 512 })))")
        assert result == {"ok": True, "value": {"v": "5.5.2", "fr": 60, "w": 512, "h": 512}}

    def test_a_gzip_bomb_is_refused(self):
        assert _read_tgs("zlib.gzipSync(Buffer.alloc(3 * 1024 * 1024, 32))") == {
            "ok": False,
            "error": "sticker animation too large",
        }

    def test_a_file_past_256_kb_packed_is_refused(self):
        result = _read_tgs("zlib.gzipSync(crypto.randomBytes(256 * 1024 + 4096))")
        assert not result["ok"] and "too large" in result["error"]

    def test_bytes_that_are_not_gzip_are_refused(self):
        assert not _read_tgs("Buffer.from(JSON.stringify({ v: '5.5.2' }))")["ok"]


def _sanitize(data: Any) -> dict:
    program = (
        f"const data = {json.dumps(data)};\n"
        "try { console.log(JSON.stringify({ ok: true, value: sanitizeLottie(data) })) }\n"
        "catch (error) { console.log(JSON.stringify({ ok: false, error: String(error.message || error) })) }"
    )
    return _run(("const LOTTIE_REFUSED_LAYERS = ", "const sanitizeLottie = (data) =>"), program)


SHAPE_LAYER = {"ty": 4, "ind": 1, "shapes": []}


@needs_node
class TestSanitizeLottie:
    """Nothing in a sender's Lottie file may make lottie-web load a font, a script or a picture."""

    def test_fonts_glyphs_and_image_assets_are_dropped(self):
        precomp = {"id": "comp_0", "layers": [SHAPE_LAYER]}
        result = _sanitize(
            {
                "fonts": {"list": [{"fFamily": "x", "fPath": "/static/evil.js"}]},
                "chars": [{"ch": "a"}],
                "assets": [{"id": "image_0", "u": "/media/", "p": "secret.png"}, precomp],
                "layers": [SHAPE_LAYER, {"ty": 0, "refId": "comp_0"}],
            }
        )
        assert result["ok"], result
        assert "fonts" not in result["value"] and "chars" not in result["value"]
        assert result["value"]["assets"] == [precomp]

    @pytest.mark.parametrize("layer_type", [2, 5])
    @pytest.mark.parametrize("where", ["top", "precomp"])
    def test_an_image_or_text_layer_refuses_the_sticker(self, layer_type, where):
        layers = [SHAPE_LAYER, {"ty": layer_type}]
        data = (
            {"layers": layers}
            if where == "top"
            else {"assets": [{"id": "comp_0", "layers": layers}], "layers": [{"ty": 0, "refId": "comp_0"}]}
        )
        assert _sanitize(data) == {"ok": False, "error": "unsupported sticker"}

    @pytest.mark.parametrize("value", [None, [], "x", 3])
    def test_json_that_is_not_an_object_is_refused(self, value):
        assert not _sanitize(value)["ok"]


@needs_node
def test_the_tgs_cache_stays_under_its_byte_budget_and_forgets_failures():
    program = (
        "(async () => {\n"
        "  const cache = createTgsCache(100);\n"
        "  const text = (n) => () => Promise.resolve('x'.repeat(n));\n"
        "  await cache.add('a', text(40));\n"
        "  await cache.add('b', text(40));\n"
        "  cache.get('a');\n"
        "  await cache.add('c', text(40));\n"
        "  const afterC = { urls: cache.urls(), bytes: cache.bytes() };\n"
        "  await cache.add('e', () => Promise.reject(new Error('gone'))).catch(() => {});\n"
        "  await new Promise(resolve => setTimeout(resolve, 0));\n"
        "  console.log(JSON.stringify({ afterC, afterE: cache.urls() }));\n"
        "})()"
    )
    result = _run(("const createTgsCache = (maxBytes) =>",), program)
    # 'a' was read after 'b', so 'b' is the oldest and goes first.
    assert result == {"afterC": {"urls": ["a", "c"], "bytes": 80}, "afterE": ["a", "c"]}


_BUDGET_PRELUDE = """
const log = [];
let reduced = false;
const budget = createStickerBudget({
  max: 4,
  reducedMotion: () => reduced,
  play: (item, once) => log.push((once ? 'once ' : 'play ') + item),
  hold: (item) => log.push('hold ' + item),
});
const step = (fn) => { log.length = 0; fn(); return { log: log.slice(), playing: budget.playing() } };
"""


def _budget(epilogue: str) -> Any:
    return _run(("const createStickerBudget = ({ max, reducedMotion, play, hold }) =>",), _BUDGET_PRELUDE + epilogue)


@needs_node
class TestStickerBudget:
    def test_at_most_four_play_and_the_newest_take_the_slots(self):
        result = _budget(
            "const entered = step(() => ['a', 'b', 'c', 'd', 'e', 'f'].forEach(item => budget.enter(item)));\n"
            "const left = step(() => budget.leave('c'));\n"
            "const pressed = step(() => budget.press('a'));\n"
            "console.log(JSON.stringify({ entered, left, pressed }))"
        )
        assert result["entered"]["playing"] == ["c", "d", "e", "f"]
        assert result["left"] == {"log": ["hold c", "play b"], "playing": ["d", "e", "f", "b"]}
        assert result["pressed"] == {"log": ["hold d", "play a"], "playing": ["e", "f", "b", "a"]}

    def test_reduced_motion_plays_nothing_and_a_press_plays_once(self):
        result = _budget(
            "reduced = true;\n"
            "const entered = step(() => ['a', 'b'].forEach(item => budget.enter(item)));\n"
            "const pressed = step(() => budget.press('b'));\n"
            "const done = step(() => budget.done('b'));\n"
            "console.log(JSON.stringify({ entered, pressed, done }))"
        )
        assert result == {
            "entered": {"log": [], "playing": []},
            "pressed": {"log": ["once b"], "playing": ["b"]},
            "done": {"log": [], "playing": []},
        }


class TestStickerTemplate:
    @staticmethod
    def _tag(marker: str) -> str:
        start = HTML.rindex("<", 0, HTML.index(marker))
        return HTML[start : HTML.index(">", HTML.index(marker)) + 1]

    def test_the_sticker_branch_comes_before_the_video_branch(self):
        """A video-sticker row must reach the sticker box, not the video player."""
        sticker = HTML.index('<div v-else-if="stickerKind(msg)"')
        video = HTML.index("<div v-else-if=\"msg.media?.type === 'video'\"")
        assert sticker < video
        assert "🎭 Animated Sticker" not in HTML

    def test_a_video_sticker_is_a_muted_loop_with_no_controls(self):
        tag = self._tag('class="gif-video sticker-video sticker-media"')
        assert tag.startswith("<video")
        for attribute in (" muted", " loop", " playsinline", 'preload="metadata"', ":data-src="):
            assert attribute in tag
        assert "controls" not in tag and ":src=" not in tag

    def test_the_tgs_element_is_empty_and_reports_a_failed_load(self):
        tag = self._tag('class="tgs-sticker sticker-media"')
        assert '@stickerfail="handleMediaError($event, msg)"' in tag
        assert HTML[HTML.index(tag) + len(tag) :].startswith("</div>")

    def test_the_fallback_label_downloads_only_when_allowed(self):
        link = self._tag('class="sticker-fallback ')
        assert link.startswith('<a v-else-if="!noDownload && getMediaUrl(msg)"')
        assert ":href=\"getMediaUrl(msg) + '?download=1'\" download" in link

    def test_a_missing_lottie_player_falls_back_to_the_label(self):
        """The player script is loaded lazily from 'self' (CSP); without it .tgs shows its label."""
        start = HTML.index("const loadLottie = () => {")
        body = HTML[start : HTML.index("\n                const ", start + 10)]
        assert "script.src = LOTTIE_SRC" in body
        assert "stickerSupport.tgs = false" in body
        assert "const LOTTIE_SRC = '/static/vendor/" in HTML
        assert "const stickerSupport = Vue.reactive(" in HTML

    def test_reduced_motion_holds_a_video_sticker_in_the_gif_observer(self):
        start = HTML.index("const setupGifObserver = () => {")
        body = HTML[start : HTML.index("}, { threshold: 0.1 })", start)]
        guard = "if (video.classList.contains('sticker-video') && prefersReducedMotion()) return"
        assert body.index("video.src = video.dataset.src") < body.index(guard) < body.index("video.play()")

    def test_the_player_follows_every_message_render(self):
        start = HTML.index("// Watch for messages changes to observe new GIFs")
        assert "syncStickers()" in HTML[start : start + 600]
        assert "anim.setSubframe(false)" in HTML
        assert "player.anim?.destroy()" in HTML
