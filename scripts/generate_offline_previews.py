#!/usr/bin/env python3
"""Opt-in offline media build; never imported by the MCP runtime.

Build dependencies: existing project Pillow, fonts-dejavu-core, and distro
librlottie0-1 (C API; librlottie-dev is optional for inspecting the API). Example:
  .venv/bin/python scripts/generate_offline_previews.py
  .venv/bin/python scripts/generate_offline_previews.py --check

No browser, network, source execution, downloaded renderer, or runtime dependency
is needed. Each native render runs in a CPU/memory/time-bounded child process.
Failure leaves the published report unchanged; there is no placeholder fallback.
"""
import argparse
import collections
import ctypes
import ctypes.util
import hashlib
import io
import json
import math
import os
from pathlib import Path
import resource
import subprocess
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat

from ingest_useanimations import lottie_safe, safe_path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = "assets/offline-visuals"
REPORT = DATA / "offline-preview-report.json"
CC0 = "licenses/offline/CC0.txt"
MAX_BYTES = 512 * 1024
MAX_PIXELS = 16_000_000
MAX_JSON = 2 * 1024 * 1024
SIZE = 192
MAX_FRAMES = 48
FPS = 10
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def sha(body):
    return hashlib.sha256(body).hexdigest()


def atomic(path, body):
    staging = path.with_name(path.name + ".pending")
    if staging.is_symlink():
        raise ValueError("symlink staging path")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        staging.write_bytes(body)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def read_json(path):
    if path.stat().st_size > MAX_JSON:
        raise ValueError(f"JSON byte cap: {path}")
    return json.loads(path.read_bytes(), parse_constant=lambda x: (_ for _ in ()).throw(
        ValueError(f"nonfinite JSON: {x}")))


def validate_lottie(value):
    if not lottie_safe(value):
        raise ValueError("not top-level Lottie animation data")
    if not isinstance(value["v"], str) or not isinstance(value["layers"], list):
        raise ValueError("invalid Lottie version/layers")
    for key in ("fr", "ip", "op", "w", "h"):
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError(f"invalid Lottie {key}")
    if not (0 < value["fr"] <= 120 and 0 <= value["ip"] < value["op"] <= 1200
            and 0 < value["w"] <= 2048 and 0 < value["h"] <= 2048
            and (value["op"] - value["ip"]) / value["fr"] <= 30):
        raise ValueError("Lottie dimension/duration cap")
    nodes = 0
    stack = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > 200_000 or depth > 64:
            raise ValueError("Lottie structural complexity cap")
        if isinstance(node, dict):
            # These icons need only vectors/precompositions. Reject even embedded
            # images and text: native loaders must have nothing external to load.
            if node.get("u") or isinstance(node.get("p"), str):
                raise ValueError("external/embedded media path forbidden")
            if node.get("ty") in (2, 5) and "ks" in node:
                raise ValueError("image/text layers unsupported")
            if any(k in node for k in ("fonts", "chars")):
                raise ValueError("fonts unsupported")
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)


def keyed_animation(value):
    if isinstance(value, dict):
        if value.get("a") == 1 and isinstance(value.get("k"), list) and len(value["k"]) > 1:
            return True
        return any(keyed_animation(v) for v in value.values())
    return isinstance(value, list) and any(keyed_animation(v) for v in value)


def has_expressions(value):
    if isinstance(value, dict):
        return isinstance(value.get("x"), str) or any(has_expressions(v) for v in value.values())
    return isinstance(value, list) and any(has_expressions(v) for v in value)


def verify_media(body, expected):
    if len(body) > MAX_BYTES:
        raise ValueError("media byte cap")
    hashes, dimensions, durations = [], [], []
    with Image.open(io.BytesIO(body)) as image:
        if image.format.lower() != expected:
            raise ValueError("incorrect media format")
        for i in range(getattr(image, "n_frames", 1)):
            image.seek(i)
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("media pixel cap")
            image.load()
            hashes.append(sha(image.convert("RGB").tobytes()))
            dimensions.append([image.width, image.height])
            durations.append(image.info.get("duration", 0))
    return {"bytes": len(body), "frames": len(hashes), "distinct_frames": len(set(hashes)),
            "dimensions": dimensions[0], "decoded_frame_sha256": hashes,
            "durations_ms": durations}


def native_render(source):
    resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_STACK, (1024 * 1024, 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    raw = source.read_bytes()
    value = read_json(source)
    validate_lottie(value)
    library = ctypes.util.find_library("rlottie")
    if not library:
        raise RuntimeError("missing optional build library: distro librlottie0-1")
    lib = ctypes.CDLL(library)
    pointer, count = ctypes.c_void_p, ctypes.c_size_t
    lib.lottie_animation_from_data.argtypes = [ctypes.c_char_p] * 3
    lib.lottie_animation_from_data.restype = pointer
    lib.lottie_animation_destroy.argtypes = [pointer]
    lib.lottie_animation_get_totalframe.argtypes = [pointer]
    lib.lottie_animation_get_totalframe.restype = count
    lib.lottie_animation_render.argtypes = [
        pointer, count, ctypes.POINTER(ctypes.c_uint32), count, count, count]
    animation = lib.lottie_animation_from_data(raw, sha(raw).encode(), b"")
    if not animation:
        raise ValueError("rlottie rejected source")
    try:
        total = lib.lottie_animation_get_totalframe(animation)
        if not 1 <= total <= 1200:
            raise ValueError("renderer frame cap")
        duration = (value["op"] - value["ip"]) / value["fr"]
        n = min(MAX_FRAMES, total, max(2, math.ceil(duration * FPS)))
        indices = sorted({round(i * (total - 1) / (n - 1)) for i in range(n)})
        buffer = (ctypes.c_uint32 * (SIZE * SIZE))()

        def frame(index):
            ctypes.memset(buffer, 0, ctypes.sizeof(buffer))
            lib.lottie_animation_render(animation, index, buffer, SIZE, SIZE, SIZE * 4)
            rgba = Image.frombytes("RGBA", (SIZE, SIZE), bytes(buffer), "raw", "BGRA")
            red, green, blue, alpha = rgba.split()
            white = ImageChops.invert(alpha)
            return Image.merge("RGB", tuple(ImageChops.add(c, white) for c in (red, green, blue)))

        frames = [frame(i) for i in indices]
        frame_hashes = [sha(f.tobytes()) for f in frames]
        if keyed_animation(value) and len(set(frame_hashes)) < 2:
            # Verify all source frames before reporting a genuine unsupported
            # animation, never silently substituting an invented/static GIF.
            if any(sha(frame(i).tobytes()) != frame_hashes[0] for i in range(total)):
                raise ValueError("sampling missed animation; choose a denser sampling policy")
            raise ValueError("keyframed source rendered static; native feature unsupported")
        ink = [sum(ImageStat.Stat(ImageChops.invert(f)).sum) for f in frames]
        if max(ink) < 100:
            raise ValueError("empty raster render")
        still_index = ink.index(max(ink))
        webp = io.BytesIO()
        frames[still_index].save(webp, format="WEBP", lossless=True, method=6)
        # One shared palette avoids per-frame quantization flicker. GIF timing
        # follows the full source interval at <=10fps, capped to 48 samples.
        strip = Image.new("RGB", (SIZE, SIZE * len(frames)), "white")
        for i, f in enumerate(frames):
            strip.paste(f, (0, i * SIZE))
        palette = strip.quantize(colors=128)
        gif_frames = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
        milliseconds = max(100, round(duration * 1000 / len(frames) / 10) * 10)
        gif = io.BytesIO()
        animated = len(set(frame_hashes)) > 1
        if animated:
            gif_frames[0].save(gif, format="GIF", save_all=True, append_images=gif_frames[1:],
                               duration=milliseconds, loop=0, disposal=2, optimize=False)
        wb, gb = webp.getvalue(), gif.getvalue() if animated else None
        validation = {"webp": verify_media(wb, "webp")}
        if gb:
            validation["gif"] = verify_media(gb, "gif")
        if keyed_animation(value) and validation.get("gif", {}).get("distinct_frames", 0) < 2:
            raise ValueError("GIF encoding lost animation")
        return wb, gb, {
            "source_sha256": sha(raw), "sample_frames": indices,
            "source_sample_times_seconds": [i / value["fr"] for i in indices],
            "frame_sha256": frame_hashes, "still_source_frame": indices[still_index],
            "source_frame_count": total, "source_frame_rate": value["fr"],
            "source_in_point": value["ip"], "source_out_point": value["op"],
            "source_keyframed": keyed_animation(value), "render_method": "distro librlottie C API",
            "source_expressions_ignored": has_expressions(value),
            "validation": validation,
        }
    finally:
        lib.lottie_animation_destroy(animation)


def font(size):
    return ImageFont.truetype("DejaVuSans.ttf", size=size)


def wrapped(text, face, width):
    lines, line = [], ""
    for word in text.split():
        candidate = (line + " " + word).strip()
        if line and face.getlength(candidate) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def diagram(entry):
    image = Image.new("RGB", (800, 520), "#f5f2ea")
    draw = ImageDraw.Draw(image)
    ink, muted, accent = "#23343b", "#52646a", "#166f78"
    categories = set(entry.get("categories", []))
    ident = entry["id"]
    tags = set(entry.get("tags", []))
    archive = ("source" in ident or "metadata" in ident or "flags" in ident
               or "source-reference" in categories or "code-reference" in tags)
    if ident.startswith("linked-"):
        archive = True
    if archive:
        mode = "archive"
    elif "navigation" in categories:
        mode = "navigation"
    elif categories & {"typography", "content", "copywriting"}:
        mode = "typography"
    elif categories & {"forms", "settings", "dialogs", "accordion"}:
        mode = "forms"
    elif categories & {"animation", "motion", "loading", "interaction", "visual-effects"}:
        mode = "motion"
    elif categories & {"design-tokens", "color", "theming"}:
        mode = "tokens"
    elif categories & {"dashboard", "tables"}:
        mode = "data"
    else:
        mode = "structure"
    draw.text((32, 22), "OFFLINE DESIGN STUDY / " + mode.upper(), fill=accent, font=font(15))
    title = wrapped(entry["title"], font(23), 736)[:2]
    for i, line in enumerate(title):
        draw.text((32, 50 + i * 27), line, fill=ink, font=font(23))
    y = 125

    def box(rect, label):
        draw.rounded_rectangle(rect, radius=7, fill="#ffffff", outline=muted, width=2)
        size = 17
        while size > 11 and font(size).getlength(label) > rect[2] - rect[0] - 24:
            size -= 1
        draw.text((rect[0] + 12, rect[1] + 12), label, fill=ink, font=font(size))

    if mode == "archive":
        assets = entry.get("assets", [])
        formats = sorted({Path(a["path"]).suffix.lstrip(".").upper() or "TEXT" for a in assets})
        box((34, y, 270, 325), "LOCAL REFERENCE ARCHIVE")
        for i, label in enumerate((formats or ["METADATA", "ORIGINAL GUIDANCE"])[:5]):
            draw.text((53, y + 55 + i * 24), label + " / inert reference", fill=muted, font=font(15))
        draw.line((290, 225, 365, 225), fill=accent, width=3)
        box((385, y, 760, 210), "Read + verify provenance")
        box((385, 240, 760, 325), "Adapt independently; do not execute")
        draw.text((34, 344), "Archive structure, not a UI screenshot or playable animation", fill=accent, font=font(17))
    elif mode == "navigation":
        box((34, y, 330, 347), "Compact window")
        draw.rectangle((50, 283, 312, 329), fill="#d5e7e5")
        draw.text((68, 295), "Home     Browse     Saved", fill=ink, font=font(17))
        box((390, y, 760, 347), "Expanded window")
        draw.rectangle((406, 175, 481, 329), fill="#d5e7e5")
        for i, label in enumerate(["Home", "Browse", "Saved"]):
            draw.text((414, 193 + i * 40), label, fill=ink, font=font(13))
        draw.text((499, 206), "Task content", fill=muted, font=font(20))
        draw.text((34, 365), "Preserve selection, reading order, and keyboard focus", fill=accent, font=font(17))
    elif mode == "typography":
        draw.text((35, y), "One clear reading hierarchy", fill=ink, font=font(37))
        draw.text((35, y + 60), "A supporting line adds context, not competing emphasis.", fill=muted, font=font(20))
        for i, length in enumerate([665, 630, 680, 450]):
            draw.line((35, y + 117 + i * 22, length, y + 117 + i * 22), fill=muted, width=3)
        draw.text((35, 352), "Display / supporting text / comfortable body measure", fill=accent, font=font(18))
    elif mode == "forms":
        box((35, y, 460, 370), "Explicit labels and useful validation")
        draw.text((53, 180), "Email address", fill=ink, font=font(17))
        box((53, 211, 440, 255), "name@example.com")
        draw.text((53, 270), "Explain the correction beside the field.", fill=muted, font=font(15))
        box((53, 315, 185, 355), "Continue")
        draw.text((496, 172), "Label", fill=accent, font=font(24))
        draw.text((496, 218), "Input", fill=accent, font=font(24))
        draw.text((496, 264), "Feedback", fill=accent, font=font(24))
        draw.text((496, 310), "Focus / action", fill=accent, font=font(24))
    elif mode == "motion":
        for i, label in enumerate(["Rest", "Feedback", "Settled"]):
            x = 35 + i * 252
            box((x, y, x + 225, 330), label)
            radius = [18, 32, 23][i]
            draw.ellipse((x + 108 - radius, 235 - radius, x + 108 + radius, 235 + radius),
                         outline=accent, width=4)
            if i < 2:
                draw.text((x + 231, 223), ">", fill=accent, font=font(22))
        draw.text((35, 355), "Concept states only — no upstream animation bundled here", fill=accent, font=font(17))
        draw.text((35, 385), "Reduced motion: show the useful settled state without transitions.", fill=muted, font=font(16))
    elif mode == "tokens":
        for i, (label, color) in enumerate([("Surface", "#ffffff"), ("Ink", ink), ("Accent", accent), ("Muted", muted)]):
            x = 35 + 187 * i
            draw.rectangle((x, y, x + 155, y + 105), fill=color, outline=muted)
            draw.text((x, y + 120), label, fill=ink, font=font(22))
        draw.text((35, 325), "Roles before decoration; verify contrast after changing tokens.", fill=accent, font=font(17))
    elif mode == "data":
        box((35, y, 760, 369), "Task-first information hierarchy")
        for i, label in enumerate(["Status", "Owner", "Next action"]):
            draw.text((60 + i * 225, 179), label, fill=accent, font=font(19))
        for i in range(4):
            yy = 219 + i * 32
            draw.line((52, yy, 740, yy), fill="#ced8d8", width=2)
            draw.text((62, yy + 6), ["Ready", "Review", "Waiting", "Complete"][i], fill=muted, font=font(15))
            draw.text((510, yy + 6), "Inspect details", fill=muted, font=font(15))
    else:
        box((35, y, 310, 345), "Primary task")
        box((350, y, 760, 225), "Context / supporting evidence")
        box((350, 250, 760, 345), "Clear next action")
        draw.text((35, 365), "Semantic structure before visual styling", fill=accent, font=font(19))
    summary = wrapped(entry.get("summary", ""), font(15), 736)[:2]
    for i, line in enumerate(summary):
        draw.text((32, 416 + i * 20), line, fill=muted, font=font(15))
    draw.line((32, 468, 768, 468), fill=muted)
    draw.text((32, 482), "Original concept illustration — not upstream screenshot", fill=ink, font=font(18))
    body = io.BytesIO()
    image.save(body, format="WEBP", lossless=True, method=6)
    verify_media(body.getvalue(), "webp")
    return body.getvalue(), mode


def descriptor(entry, path, fmt, origin, title):
    original = origin == "original"
    return {"id": "offline-animation-preview" if fmt == "gif" else "offline-preview",
            "path": path, "mime_type": "image/" + fmt, "title": title,
            "origin": origin, "format": fmt,
            "license": "CC0-1.0" if original else entry["license"],
            "license_path": CC0 if original else entry["license_path"],
            "source_url": "original://designerMCP/offline/" + entry["id"] if original else entry["source_url"]}


def entries():
    result = {}
    for path in sorted(DATA.glob("*library.json")):
        for entry in json.loads(path.read_bytes())["entries"]:
            if entry["id"] in result:
                raise ValueError("duplicate reference")
            result[entry["id"]] = entry
    return result


def check(report):
    kinds = collections.Counter()
    for ident, record in report["references"].items():
        kinds[record["kind"]] += 1
        for asset in record["assets"]:
            path = safe_path(asset["path"])
            body = path.read_bytes()
            if sha(body) != report["artifact_hashes"][asset["path"]]:
                raise ValueError("artifact hash changed: " + str(path))
            verified = verify_media(body, asset["format"])
            if (asset["format"] == "gif" and record["source_keyframed"]
                    and verified["distinct_frames"] < 2):
                raise ValueError("static animation: " + ident)
            if not safe_path(asset["license_path"]).is_file():
                raise ValueError("missing license")
        if record["kind"] in {"rendered-animation", "unsupported-animation"}:
            if sha(safe_path(record["source_path"]).read_bytes()) != record["source_sha256"]:
                raise ValueError("source hash changed")
    if set(report["references"]) != set(entries()):
        raise ValueError("reference coverage mismatch")
    return dict(kinds)


def build():
    refs, hashes = {}, {}
    for ident, entry in sorted(entries().items()):
        if not ident or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in ident):
            raise ValueError("unsafe reference id")
        lotties = []
        for asset in entry.get("assets", []):
            if asset["path"].endswith(".json"):
                value = read_json(safe_path(asset["path"]))
                if isinstance(value, dict) and all(k in value for k in ("v", "fr", "ip", "op", "layers")):
                    validate_lottie(value)
                    lotties.append(asset)
        if len(lotties) > 1:
            raise ValueError("multiple animation sources require explicit selection")
        base = f"{OUT}/{ident}"
        if lotties:
            source = lotties[0]
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--worker", source["path"]],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40,
                env={**os.environ, "OMP_NUM_THREADS": "1"}, check=False)
            if completed.returncode:
                raise RuntimeError(f"{ident}: renderer failed: " + completed.stderr.decode()[-1500:])
            result = json.loads(completed.stdout)
            # Worker uses hex encoding to avoid filesystem handoff or native
            # output paths. Parent is the only publisher of validated artifacts.
            assets = []
            for fmt in ("webp", "gif"):
                encoded = result.pop(fmt)
                path = base + "." + fmt
                if encoded is None:
                    safe_path(path).unlink(missing_ok=True)
                    continue
                body = bytes.fromhex(encoded)
                verify_media(body, fmt)
                atomic(safe_path(path), body)
                hashes[path] = sha(body)
                assets.append(descriptor(entry, path, fmt, "rendered",
                                         entry["title"] + (" — low-FPS source preview" if fmt == "gif" else " — rendered still")))
            limitations = ["Low-FPS raster preview; use original Lottie JSON for vector fidelity."]
            if result["source_expressions_ignored"]:
                limitations.append("rlottie renders encoded keyframes only; source JavaScript expressions remain inert and are ignored.")
            if "gif" not in result["validation"]:
                limitations.append("UNSUPPORTED ANIMATION: expression-only source has no encoded motion; no static GIF is presented as animation success.")
            refs[ident] = {"assets": assets,
                           "kind": "rendered-animation" if "gif" in result["validation"] else "unsupported-animation",
                           "animation_source_asset_id": source["id"], "source_path": source["path"],
                           "limitations": limitations,
                           **result}
        else:
            previews = [a for a in entry.get("assets", []) if a["mime_type"] == "image/webp"
                        and "preview" in a["id"]]
            if previews:
                source = previews[0]
                body = safe_path(source["path"]).read_bytes()
                verify_media(body, "webp")
                path = base + ".webp"
                atomic(safe_path(path), body)
                asset = descriptor(entry, path, "webp", "upstream", source["title"])
                refs[ident] = {"assets": [asset], "kind": "upstream-preview", "limitations": [],
                               "upstream_source_path": source["path"], "source_sha256": sha(body)}
            else:
                body, mode = diagram(entry)
                path = base + ".webp"
                atomic(safe_path(path), body)
                asset = descriptor(entry, path, "webp", "original", "Original concept illustration — not upstream screenshot")
                refs[ident] = {"assets": [asset], "kind": "original-concept", "diagram_category": mode,
                               "limitations": ["Original explanatory diagram, not an upstream UI screenshot or animation.",
                                               "Archived source/metadata remains inert; no HTML, SVG, or code executed."]}
            hashes[path] = sha(body)
    report = {"version": 1, "references": refs, "artifact_hashes": hashes,
              "render_dependencies": {"optional_build_only": ["Pillow", "distro librlottie0-1", "fonts-dejavu-core"],
                                      "runtime_dependencies_added": False},
              "limitations": ["121 reference visuals are original concepts rather than upstream screenshots.",
                              "Only true locally stored Lottie data warrants animated GIF previews.",
                              "useanimations-error: expression-only wiggle motion unsupported by rlottie; real still only, no fake/static animation fallback.",
                              "Raster/native rendering may differ from web/Flutter Lottie renderers."]}
    report["stats"] = {"references": len(refs), "artifacts": len(hashes), "kinds": check(report)}
    report["stats"]["animated_previews"] = sum(
        any(a["format"] == "gif" for a in r["assets"]) for r in refs.values())
    atomic(REPORT, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
    print(json.dumps(report["stats"], sort_keys=True))
    if report["stats"]["kinds"].get("unsupported-animation", 0):
        print("Incomplete animation coverage: see explicit UNSUPPORTED ANIMATION limitations.", file=sys.stderr)
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Decode published assets and check source/artifact hashes")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        wb, gb, info = native_render(safe_path(args.worker))
        print(json.dumps({"webp": wb.hex(), "gif": gb.hex() if gb else None, **info}))
    elif args.check:
        print(json.dumps(check(json.loads(REPORT.read_bytes())), sort_keys=True))
    else:
        build()


if __name__ == "__main__":
    main()
