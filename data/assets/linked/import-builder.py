"""Bounded, explicit supplemental import. Run from the project root; never execute downloads."""
import datetime
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path("data")
FILE_CAP = 250_000
TOTAL_CAP = 25_000_000
ALLOWED = {"api.github.com", "raw.githubusercontent.com"}
transferred = 0
files = []
gaps = []


class PublicHTTPS(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED or parts.port not in (None, 443):
        raise ValueError("HTTPS public allowlist violation")
    if parts.username or parts.password:
        raise ValueError("credentials forbidden")
    for result in socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(result[4][0]).is_global:
            raise ValueError("non-public DNS destination")


def fetch(url):
    global transferred
    check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "offline-designer-bounded-license-import"})
    with urllib.request.build_opener(PublicHTTPS()).open(req, timeout=20) as response:
        check_url(response.url)
        payload = response.read(FILE_CAP + 1)
    transferred += len(payload)
    if len(payload) > FILE_CAP or transferred > TOTAL_CAP:
        raise ValueError("download byte budget exceeded")
    return payload


def save(payload, relative, url, revision, license_name, kind, source_path):
    target = ROOT / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    record = {
        "path": relative, "source_url": url, "revision": revision,
        "upstream_path": source_path, "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload), "license": license_name, "kind": kind,
        "modified": False,
    }
    files.append(record)
    return relative


# Each selection is code, not artwork or models. Pinned source files are offline reading
# references, not self-contained vendored runtimes. No dependencies are fetched.
SELECTIONS = {
    "hyperframes": ("heygen-com/hyperframes", "Apache-2.0", "LICENSE",
                    ["examples/aws-lambda/README.md", "examples/gcp-cloud-run/README.md"]),
    "anime": ("juliangarnier/anime", "MIT", "LICENSE.md",
              ["src/easings/linear/index.js", "src/easings/cubic-bezier/index.js"]),
    "react-spring": ("pmndrs/react-spring", "MIT", "LICENSE",
                     ["packages/core/src/hooks/useSpring.ts", "demo/src/sandboxes/chain/src/App.tsx"]),
    "pixijs": ("pixijs/pixijs", "MIT", "LICENSE",
               ["src/maths/point/Point.ts", "src/maths/point/ObservablePoint.ts"]),
    "p5-js": ("processing/p5.js", "LGPL-2.1", "license.txt",
              ["src/math/trigonometry.js", "src/math/calculation.js"]),
    "paper-js": ("paperjs/paper.js", "MIT", "LICENSE.txt",
                 ["src/basic/Point.js", "src/basic/Size.js"]),
    "matter-js": ("liabru/matter-js", "MIT", "LICENSE",
                  ["examples/constraints.js", "examples/gravity.js"]),
    "three-js": ("mrdoob/three.js", "MIT", "LICENSE",
                 ["src/math/MathUtils.js", "src/math/Vector2.js"]),
    "rapier": ("dimforge/rapier", "Apache-2.0", "LICENSE",
               ["examples2d/damping2.rs", "examples2d/restitution2.rs"]),
    "ogl": ("oframe/ogl", "Unlicense", "README.md",
            ["examples/triangle-screen-shader.html", "src/math/Vec2.js"]),
    "tsparticles": ("tsparticles/tsparticles", "MIT", "LICENSE",
                    ["presets/confetti/src/options.ts", "presets/stars/src/options.ts"]),
    "gltf-transform": ("donmccurdy/glTF-Transform", "MIT", "LICENSE.md",
                       ["packages/functions/src/center.ts", "packages/functions/src/get-bounds.ts"]),
    "gl-transitions": ("gl-transitions/gl-transitions", "MIT", "LICENSE",
                       ["transitions/Fade.glsl", "transitions/DirectionalWipe.glsl"]),
    "tone-js": ("Tonejs/Tone.js", "MIT", "LICENSE.md",
                ["Tone/core/type/Time.ts", "Tone/core/type/Frequency.ts"]),
}

NO_SOURCE = {
    "chatterbox": ("resemble-ai/chatterbox", "MIT", "LICENSE"),
    "faster-whisper": ("SYSTRAN/faster-whisper", "MIT", "LICENSE"),
    "opencut": ("OpenCut-app/OpenCut", "MIT", "LICENSE"),
    "obs-studio": ("obsproject/obs-studio", "GPL-2.0", "COPYING"),
    "ffmpeg": ("FFmpeg/FFmpeg", "build-dependent LGPL/GPL", "LICENSE.md"),
}

PINNED = {
    "heygen-com/hyperframes": "e46ecd850ec49fb2d97d8e4cca71ab30dc1849e2",
    "juliangarnier/anime": "01b81be1df6843ccfe0a71c0699a746bf740dd77",
    "pmndrs/react-spring": "83e476061290532ab517c4d4dfd60de21589e78e",
    "pixijs/pixijs": "4fabfa6409c35cc114cb4fbfbb812c9cd8abdced",
    "processing/p5.js": "3dd088659b733505f0be9d6eb3f7a61e8142b9f6",
    "paperjs/paper.js": "f8550b13446b68b8de256f627f28a36737a2a18c",
    "liabru/matter-js": "b8ee71cd7f9b49f53639ddaedd6254f1929bcbe5",
    "mrdoob/three.js": "c6a4b78be1a3d7839399410cc157950c5fd6e58b",
    "dimforge/rapier": "475caa3ff4646fd9a796b67d14116256f4962bc7",
    "oframe/ogl": "385ce65c352c70734a36dc98c787fdd1d30ddb3b",
    "tsparticles/tsparticles": "19821ac564e3314ae3d188af2778597b8423693b",
    "donmccurdy/glTF-Transform": "8e2a94a2616d293c4b503d0fb6a4b42e6ec7c62f",
    "gl-transitions/gl-transitions": "6e235c9649233abda2d2c6cb54a4896aac649175",
    "Tonejs/Tone.js": "105c04cc00289caf49684ac5c7621bce8a042c4c",
    "obsproject/obs-studio": "7a25c3fd8b5eb87c2d2e0606cae36e32014bc6fa",
    "resemble-ai/chatterbox": "5de7a54aa4e5e2baadb0182dde554908b48b85c2",
    "SYSTRAN/faster-whisper": "2a3a857737d17d974f30d818ebec69ce11929e5c",
    "OpenCut-app/OpenCut": "e668010778568641babef2cc40be4703ae6916d6",
    "FFmpeg/FFmpeg": "3a3f5e24a95b744d237d0cb01dccb647e44b9457",
}

# Original editorial knowledge; no gallery screenshots, prompt packs or articles.
# Tuple: slug, category, platforms, tags, synopsis, application guidance, language, original example.
KNOWLEDGE = [
    ("motionsites-studio", "ai-website-builder", ["web"], ["ai", "prototype", "landing-page"],
     "An AI-assisted website customization service. Use it as a prototyping workflow reference, not as a source of redistributable templates.",
     "Start with an independently written brief: audience, task, evidence, conversion action and accessibility constraints. Separate factual business copy from speculative placeholder copy. Build a neutral wireframe first, then try one visual direction at a time. Evaluate the generated result by keyboard order, narrow-screen readability, semantic headings and useful empty/error states. Keep your own source-of-truth content outside the builder. Before exporting, verify the service's project-use terms and every font, image and video license. This archive contains no builder output or proprietary prompts.",
     "text", "Original brief: A repair shop needs a one-page booking site. Primary action: request a repair. Evidence: opening hours and verified customer quotations. Layout: concise introduction, three service cards, pricing explanation, accessible contact form. No autoplay video. At 320px, labels remain visible and actions never overlap."),
    ("motionsites", "ai-design-prompts", ["web"], ["brief", "motion", "prompting"],
     "A commercial design-prompt and motion reference library. Offline coverage is factual metadata plus independently authored design planning.",
     "Translate an observed design goal into your own constraints rather than transcribing a prompt. Specify information hierarchy, spacing rhythm, motion purpose and failure behavior. For a restrained hero, give the headline most of the visual weight and put proof near the action. Limit decorative animation to one layer that cannot obscure text. Treat AI output as a candidate requiring contrast, layout-shift and interaction tests. Product use rights do not imply permission to repackage a prompt library. Do not put purchased prompts or source packs into this corpus.",
     "css", ".hero { display: grid; gap: 1.5rem; padding: clamp(2rem, 7vw, 6rem); }\n.hero h1 { max-width: 16ch; font-size: clamp(2rem, 5vw, 4rem); line-height: 1.08; }\n.hero p { max-width: 58ch; }\n@media (min-width: 60rem) { .hero { grid-template-columns: 3fr 2fr; align-items: center; } }"),
    ("land-book", "landing-page-inspiration", ["web"], ["composition", "conversion", "inspiration"],
     "A destination described in the discovered post as a landing-page inspiration resource. Discovery received HTTP 403; current content and terms remain unverified.",
     "Because the destination could not be inspected, do not infer current catalog features, screenshot rights or pricing. You can still apply an original landing-page critique: identify the first question the visitor asks, make the answer visible above the action, then supply evidence and objections in a logical order. Use a text-only reference worksheet instead of copying a page. Check that the page remains coherent without illustrations. Choose a conversion metric tied to successful user intent rather than time spent looking at animation.",
     "text", "Independent landing-page worksheet:\n1. Audience and immediate question.\n2. One-sentence answer and primary action.\n3. Three claims with evidence owners.\n4. Common objections and truthful answers.\n5. Mobile reading order.\n6. Completion metric and accessible fallback.\nDo not attach third-party screenshots."),
    ("recent", "design-inspiration", ["web", "react", "flutter"], ["branding", "editorial", "layout"],
     "A public design-reference gallery spanning interfaces, web, branding, motion and print. Third-party work is not made reusable by being visible.",
     "Record abstract design decisions: density, alignment, typography scale, color roles and movement cadence. Distinguish brand expression from usability. Rebuild a chosen principle using your own text and geometry; do not reproduce a recognizable page or identity. On web, implement hierarchy with semantic elements. In Flutter, map the same hierarchy to text styles and constrained padding rather than screenshot dimensions. A useful comparison changes only one variable, such as card density, so feedback identifies the cause.",
     "dart", "Widget originalInfoCard(String title, String detail) => Padding(\n  padding: const EdgeInsets.all(20),\n  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [\n    Text(title, style: const TextStyle(fontSize: 24, fontWeight: FontWeight.w600)),\n    const SizedBox(height: 12), Text(detail),\n  ]),\n);"),
    ("landdding", "landing-page-inspiration", ["web"], ["gallery", "wireframe", "templates"],
     "A community website gallery with categorical and time-based browsing. Its restricted site-content terms are not an offline redistribution grant.",
     "Use references to discover questions, not to duplicate solutions. Sketch three structurally different wireframes for the same original content: proof-first, task-first and comparison-first. Test them with a reader who has not seen the brief. Keep the identity and imagery independent. Treat cloneable templates as separately licensed project inputs, never as archive assets. If an item lacks clear permission, preserve only the URL and your own factual interpretation. Match component order to the visitor's decision path.",
     "html", "<main>\n  <header><h1>Plan a calmer team week</h1><p>Make responsibilities visible before Monday.</p><a href=\"#plan\">Create a plan</a></header>\n  <section aria-labelledby=\"proof\"><h2 id=\"proof\">What changes</h2><ul><li>One owner per task</li><li>Visible review dates</li></ul></section>\n  <section id=\"plan\"><h2>Start with your next milestone</h2></section>\n</main>"),
    ("font-in-logo", "typography-reference", ["web", "flutter"], ["fonts", "identity", "typography"],
     "A catalog connecting brand logos with font references. Logo visibility grants neither trademark rights nor font-file redistribution rights.",
     "Separate typeface identification from licensing. A brand wordmark may be modified lettering rather than a stock font, and an identification can be approximate. For your own identity, compare x-height, apertures, numeral clarity and text-width at realistic sizes. Archive only independently licensed font files after checking desktop, web, app embedding and redistribution scope. Use system fonts for an offline baseline. Build a specimen with your own words, numbers, punctuation and multilingual content; test fallback and font-loading layout stability.",
     "css", ".specimen { font-family: system-ui, sans-serif; font-variant-numeric: tabular-nums; }\n.specimen__title { font-size: clamp(1.8rem, 4vw, 3.2rem); letter-spacing: -.025em; }\n.specimen__body { max-width: 65ch; line-height: 1.6; }\n/* Test with original text: Harbor 2026 — Invoice #1048; I/l/1; O/0. */"),
    ("godly", "design-inspiration", ["web"], ["redirect", "gallery", "visual-design"],
     "The discovered Godly URL redirected to Recent with a referral parameter. It is retained as a searchable alias, not claimed as an independently archived gallery.",
     "Track provenance when a reference moves: keep the original destination, the observed redirect and observation date separate. For design work, derive a neutral visual system: background, surface, accent, readable text and border tokens. Ensure an accent conveys state alongside words or icons. Do not assume two URLs imply two independent sources. This entry deliberately carries original guidance and no screenshots from either destination. Compare a token system in light and dark contexts before building elaborate motion.",
     "css", ":root { --page: #f6f7fb; --surface: #fff; --ink: #202839; --accent: #3153c7; --edge: #c7cedd; }\n.panel { color: var(--ink); background: var(--surface); border: 1px solid var(--edge); border-radius: 1rem; }\n.panel a { color: var(--accent); text-decoration: underline; text-underline-offset: .2em; }"),
    ("60fps", "interaction-inspiration", ["web", "react", "flutter", "ios", "android"], ["motion", "interaction", "accessibility"],
     "A curated UI animation reference service. Its terms prohibit mirroring, caching and redistribution through another reference product; no service content is imported.",
     "Invent a motion specification from the task itself. Define trigger, state change, duration, easing, interruption and reduced-motion behavior. Prefer animation that explains spatial continuity or confirms an action. Keep the final state usable with zero animation. For a disclosure, update semantic expanded state immediately; animate only the visual layer. For native apps, respect platform motion settings and avoid gestures as the sole path. The example is an independently designed state transition, not a service shot or breakdown.",
     "css", ".notice { opacity: 1; transform: translateY(0); transition: opacity 140ms ease-out, transform 180ms ease-out; }\n.notice[data-entering=\"true\"] { opacity: 0; transform: translateY(.4rem); }\n@media (prefers-reduced-motion: reduce) { .notice { transition: none; transform: none; } }"),
    ("modulify", "website-templates", ["web"], ["builder", "templates", "responsive"],
     "A site-template catalog with previews and dashboard cloning. Project-use permissions do not establish a right to republish catalog media or template files.",
     "Write a template acceptance checklist before choosing a layout. Inspect actual content fit, keyboard behavior, responsive overflow, loading cost and form ownership. Replace promotional placeholder claims with verified statements. Make header navigation work independently of decorative animation. Clone only through authorized product features and keep any acquired template outside this redistribution library unless the item grants redistribution explicitly. For your own reusable template, define slots and constraints instead of matching a vendor's visual identity.",
     "text", "Original reusable page contract:\nSlots: title, description, primary action, evidence list, FAQ, contact details.\nConstraints: title <= 80 characters; one primary action per section; images require alt decisions; no fixed-height copy containers.\nStates: loading, empty, error, success.\nAcceptance: 320px width, 200% zoom, keyboard-only navigation, no motion."),
    ("oversight-sections", "website-sections", ["web", "react"], ["hero", "scroll", "section-design"],
     "A commercial collection of interactive website sections. Advertising a section as free to copy does not authorize republishing the source catalog.",
     "Design a section around one user question and one explicit outcome. Use CSS layout and semantic content before adding scroll behavior. A scroll story should preserve a complete reading order without pinning or effects. Keep visual progress separate from real application progress. Do not trap focus inside sticky sections. If using an unlocked vendor template in a client project, retain the item terms; do not redistribute template files or access links here. The original example supplies a scalable section frame without recreating vendor artwork.",
     "jsx", "function EvidenceSection({ title, items }) {\n  return <section aria-labelledby=\"evidence-title\" className=\"evidence\">\n    <h2 id=\"evidence-title\">{title}</h2>\n    <ul>{items.map(item => <li key={item.id}><h3>{item.label}</h3><p>{item.detail}</p></li>)}</ul>\n  </section>;\n}"),
    ("oversight-videos", "video-production", ["web", "react"], ["storyboard", "explainer", "ai-video"],
     "A commercial collection of video-style workflow skills and references. No proprietary scripts, instructions, videos or paid content are archived.",
     "Build an original explainer by scripting the message before selecting effects. Split the message into problem, concrete change and next action; assign one idea per shot. Use independently licensed media or your own geometry. Keep voiceover and captions semantically equivalent. Allocate silence so the viewer can read numbers. For deterministic rendering, express timing in frames and record the frame rate. Obtain consent for identifiable voices and likenesses. Do not copy a premium skill or style recipe into an offline tool.",
     "json", "{\"fps\":30,\"shots\":[{\"start\":0,\"frames\":90,\"message\":\"Tasks disappear between meetings\",\"visual\":\"three original labeled cards\"},{\"start\":90,\"frames\":120,\"message\":\"Assign one owner and review date\",\"visual\":\"cards align into an original timeline\"},{\"start\":210,\"frames\":90,\"message\":\"Start with your next milestone\",\"visual\":\"plain call-to-action and captions\"}]}"),
    ("oversight-supply", "website-templates", ["web"], ["framer", "webflow", "licensing"],
     "A premium Webflow/Framer template and video-workflow supplier. This offline entry supplies original evaluation guidance, not its files or access links.",
     "Treat licensing as part of component selection. Record who purchased an item, the permitted project count, allowed platforms, transfer rules and restrictions on source sharing. Prefer your own reusable primitives for a public library. A template can be appropriate for a single client while unsuitable for redistribution in an MCP asset service. Evaluate recurring subscription and export constraints before investing in customization. Build a license ledger that separates template rights from font, stock image, plugin and customer-content rights.",
     "json", "{\"project\":\"original-client-site\",\"inputs\":[{\"kind\":\"layout\",\"origin\":\"in-house\",\"redistributable\":true},{\"kind\":\"font\",\"origin\":\"system stack\",\"filesBundled\":false},{\"kind\":\"photography\",\"origin\":\"client-provided\",\"permissionStatus\":\"verify before publication\"}],\"reviewBeforeExport\":[\"template scope\",\"font embedding\",\"stock media\",\"client consent\"]}"),
    ("refero", "ui-pattern-reference", ["web", "react", "flutter", "ios"], ["product-ui", "forms", "onboarding"],
     "A searchable web/iOS UI-reference service described by public metadata. Discovery did not expose the catalog; no screenshot or catalog redistribution permission was found.",
     "Study a pattern by writing the user's goal, information required, error recovery and success feedback. Design an original flow with fewer decisions rather than reproducing another product's screen. For forms, keep labels visible, explain validation next to the field, preserve values after errors and announce submission status. Platform-native expectations matter: web needs keyboard semantics; Flutter needs semantics labels, text scaling and accessible touch targets. Keep a text-only critique log when catalog rights are uncertain.",
     "html", "<form>\n  <label for=\"workspace\">Workspace name</label>\n  <input id=\"workspace\" name=\"workspace\" aria-describedby=\"workspace-help\" required maxlength=\"60\">\n  <p id=\"workspace-help\">Use a name your teammates recognize.</p>\n  <button type=\"submit\">Create workspace</button>\n  <p role=\"status\" aria-live=\"polite\"></p>\n</form>"),
    ("remotion", "video-production", ["react", "web"], ["rendering", "timeline", "commercial-license"],
     "A React-based programmatic video and image toolkit under the custom Remotion License, not MIT. Entity eligibility and commercial license requirements must be reviewed.",
     "Use frame-indexed rendering for reproducible motion: a visual state should be a function of frame number, composition size and explicit inputs. Avoid wall-clock timers, random values without a seed and untracked network requests while rendering. Keep preview interactivity separate from render state. Verify the current custom license for your organization before installing or deploying; the discovered free-use eligibility is not a blanket open-source grant. No Remotion source is redistributed. The independent example demonstrates frame math without importing Remotion.",
     "javascript", "export function originalShotState(frame, fps) {\n  const seconds = frame / fps;\n  const t = Math.max(0, Math.min(1, seconds / 0.6));\n  const eased = 1 - (1 - t) ** 3;\n  return { opacity: eased, translateY: 18 * (1 - eased), caption: seconds < 3 ? 'Make the next step visible' : 'Give it an owner' };\n}"),
    ("hyperframes", "video-production", ["web"], ["html-to-video", "rendering", "deployment"],
     "An HTML-to-video rendering project aimed at agent-assisted workflows. Selected Apache-2.0 deployment references are archived separately from media and dependencies.",
     "Treat a rendered HTML scene as a deterministic document, not an arbitrary website capture. Bundle original text and explicitly licensed assets, fix viewport and font availability, wait for known readiness signals and prohibit late network loads. Render a short representative scene before committing to a full export. Keep worker resource limits and job cancellation explicit. Deployment references in this archive may depend on external infrastructure; they are reading material, not automatically runnable jobs. Review notices and cloud costs separately.",
     "html", "<section class=\"scene\" aria-label=\"Original explainer frame\">\n  <h1>One task, one owner</h1><p>A clear handoff beats another status meeting.</p>\n</section>\n<style>.scene{width:1280px;height:720px;display:grid;place-content:center;background:#102437;color:#fff;font:32px system-ui;padding:80px;box-sizing:border-box}.scene h1{font-size:72px;max-width:15ch}</style>"),
    ("anime", "animation-engine", ["web", "react"], ["timeline", "easing", "svg"],
     "A JavaScript animation engine for visual choreography. Pinned MIT easing implementation references are archived; all inline examples are independently authored.",
     "Use a timeline when several visual changes share a narrative, not merely because animation is available. Define the settled state in CSS so loading failure cannot hide content. Animate transform and opacity where possible; measure layout-affecting changes. In React, scope targets to a ref and clean up the engine's active animations during unmount. Anime APIs vary by major version; inspect the pinned source and installed package before adapting. A menu should update its real state immediately and use motion only to reinforce it.",
     "javascript", "// Original browser animation recipe; no Anime runtime is bundled.\nfunction revealOriginalCard(element) {\n  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;\n  return element.animate([\n    { opacity: 0, transform: 'translateY(10px)' },\n    { opacity: 1, transform: 'translateY(0)' }\n  ], { duration: 180, easing: 'cubic-bezier(.2,.7,.2,1)' });\n}"),
    ("react-spring", "spring-animation", ["react", "web"], ["physics", "react", "state-transition"],
     "A React animation library using spring dynamics. Selected MIT demo sources are reading references; adapt APIs to the installed version.",
     "Use springs for continuity when a target changes before the previous movement finishes. Tune by interaction semantics: a dragging object should follow intent; a confirmation should settle quickly without repeated bounce. Keep state ownership in React and render the final target even if motion is disabled. Do not animate a huge list independently without profiling. Test rapid toggling and unmount while moving. Avoid fixed durations as a substitute for spring behavior; reduce-motion can select an immediate state update. The example demonstrates the conceptual model independently.",
     "javascript", "export function originalSpringStep(position, velocity, target, dt) {\n  const step = Math.min(Math.max(dt, 0), 1 / 60);\n  const acceleration = 180 * (target - position) - 24 * velocity;\n  const nextVelocity = velocity + acceleration * step;\n  return { position: position + nextVelocity * step, velocity: nextVelocity };\n}\n// Illustrative semi-implicit step; production solvers need stability testing."),
    ("pixijs", "graphics-2d", ["web", "react"], ["canvas", "gpu", "sprites"],
     "A GPU-accelerated 2D JavaScript rendering library. Archived MIT geometry source contains no demo artwork.",
     "Choose a canvas renderer for many moving visual objects, not for ordinary forms or text-heavy layouts. Maintain DOM alternatives for essential information and actions. Use logical coordinates, resize intentionally and cap device pixel ratio for expensive scenes. Batch similar objects and avoid creating textures every frame. Release textures and event listeners on teardown. Separate simulation from rendering so tests can inspect numeric state. The original coordinate helper illustrates responsive scene placement without external assets.",
     "javascript", "export function originalFitScene(width, height, designWidth = 960, designHeight = 540) {\n  const scale = Math.min(width / designWidth, height / designHeight);\n  return { scale, x: (width - designWidth * scale) / 2, y: (height - designHeight * scale) / 2 };\n}\n// Apply the result to a scene container; keep controls in accessible DOM."),
    ("p5-js", "creative-coding", ["web"], ["generative", "canvas", "lgpl"],
     "A browser creative-coding platform. Selected upstream math source is LGPL-2.1 and is retained with its actual license; it is not relabeled MIT.",
     "Prototype a visual idea using a deterministic sketch, then decide whether it belongs in the production interface. Use your own geometry and color system rather than bundled example artwork. Separate random seed, interaction inputs and rendering dimensions. Keep frame work bounded and stop loops when the scene is offscreen. For distribution of p5 or modified library source, review LGPL notice, source and relinking obligations applicable to the packaging. The independent inline sketch is not copied from upstream and does not replace those obligations.",
     "javascript", "// Original p5 global-mode sketch; requires a separately installed p5 runtime.\nfunction setup() { createCanvas(480, 240); noLoop(); }\nfunction draw() {\n  background('#f4f5fa'); noFill(); stroke('#3153c7');\n  for (let row = 0; row < 6; row++) {\n    beginShape();\n    for (let x = 0; x <= width; x += 8) vertex(x, 30 + row * 32 + 8 * sin(x / 42 + row));\n    endShape();\n  }\n}"),
    ("paper-js", "vector-graphics", ["web"], ["paths", "bezier", "canvas"],
     "A canvas vector-graphics scripting library for paths and geometry. Selected MIT numeric primitives are retained as source references.",
     "Use vectors when the important object is a path, curve, intersection or editable shape. Keep design coordinates distinct from CSS pixels. Avoid accumulating new paths on each frame; update existing objects or clear the project deliberately. Keep handles and hit regions usable at touch sizes. For editable diagrams, serialize semantic shapes in your own model instead of making the canvas the only source of truth. Exported SVG must be reviewed for external references and accessibility. The independent example derives curve controls without copying a drawing.",
     "javascript", "export function originalConnector(a, b) {\n  const bend = Math.max(24, Math.abs(b.x - a.x) * 0.45);\n  return `M ${a.x} ${a.y} C ${a.x + bend} ${a.y}, ${b.x - bend} ${b.y}, ${b.x} ${b.y}`;\n}\n// Use this path in your own SVG or vector scene; label relationships in text."),
    ("matter-js", "physics-2d", ["web"], ["rigid-body", "collision", "simulation"],
     "A browser-oriented 2D rigid-body physics engine. Two selected MIT demonstrations illustrate constraints and gravity without importing their media.",
     "Use physics for meaningful interactive objects, not as a replacement for predictable navigation. Run a fixed simulation timestep and interpolate visual state when necessary. Distinguish visual size from collision boundaries; constrain speed so objects cannot jump through thin walls. Pause or reset when the scene leaves view. Essential controls should remain outside the physics world. A celebratory pile of cards should be decorative and should not obstruct actual content. Imported demos require the corresponding runtime and are not claimed as standalone assets.",
     "javascript", "export function originalFixedStepper(step, interval = 1 / 60) {\n  let remaining = 0;\n  return elapsed => {\n    remaining = Math.min(remaining + Math.max(0, elapsed), interval * 5);\n    while (remaining >= interval) { step(interval); remaining -= interval; }\n    return remaining / interval;\n  };\n}\n// Clamp backlog to avoid a catch-up spiral after a suspended tab."),
    ("three-js", "graphics-3d", ["web", "react"], ["webgl", "scene", "3d"],
     "A JavaScript 3D rendering library. Archived MIT math source is separate from models, textures and third-party example assets.",
     "Introduce 3D only when spatial understanding improves the task. Budget mesh count, draw calls, texture memory and pixel ratio before adding effects. Use an original primitive scene for a performance baseline. Dispose of geometries, materials and textures when replacing scenes. Provide a meaningful fallback image or text and DOM controls for interaction. Asset licenses are independent of the renderer license: processing a model with Three.js does not grant rights to redistribute it. The original orbit helper is library-independent numeric guidance.",
     "javascript", "export function originalOrbitPose(angle, radius, height) {\n  return { x: Math.cos(angle) * radius, y: height, z: Math.sin(angle) * radius };\n}\nexport function originalPixelRatio(deviceRatio) { return Math.min(2, Math.max(1, deviceRatio || 1)); }\n// Apply a bounded ratio after measuring low-end GPU frame times."),
    ("rapier", "physics-3d", ["web"], ["wasm", "physics", "collision"],
     "A high-performance 2D/3D physics engine. Selected Apache-2.0 Rust examples cover damping and restitution; web bindings and dependencies are not bundled.",
     "Choose the 2D or 3D engine intentionally and match units across graphics and simulation. Start with gravity, one fixed collider and one dynamic body. Test collision groups, sleeping and timestep behavior before integrating character movement. WASM initialization is asynchronous; do not render interactive controls as ready before the engine is available. Keep simulation state deterministic where practical, but do not assume identical floating-point results across every platform. Review notices before redistributing compiled bindings. The inline model is an original design of world parameters, not a copied Rapier sample.",
     "json", "{\"units\":\"meters\",\"dimension\":3,\"fixedStepSeconds\":0.0166666667,\"gravity\":[0,-9.81,0],\"interactiveObjects\":[{\"id\":\"original-ball\",\"shape\":\"sphere\",\"radius\":0.25,\"position\":[0,2,0],\"restitution\":0.2}],\"fallback\":\"static diagram and textual explanation\",\"pauseWhenHidden\":true}"),
    ("ogl", "webgl-shaders", ["web"], ["webgl", "shader", "minimal"],
     "A small WebGL library. Its repository README contains the Unlicense dedication; that actual pinned evidence is retained rather than inventing a missing LICENSE file.",
     "A minimal graphics wrapper offers control but leaves more lifecycle, shader and compatibility decisions to you. Build one triangle before layering effects. Define shader uniforms explicitly and check precision support. Compile failures should produce a usable DOM fallback, not a blank page. Resize the drawing buffer separately from CSS size and clean up GPU resources. Read archived HTML as inert source: external scripts and assets are not fetched or executed. Use original procedural geometry so media licensing does not become ambiguous.",
     "glsl", "// Original fragment body; host supplies vUv and uTime.\nprecision mediump float;\nvarying vec2 vUv;\nuniform float uTime;\nvoid main() {\n  float band = 0.5 + 0.5 * sin(vUv.x * 7.0 + uTime * 0.25);\n  vec3 base = mix(vec3(0.08,0.15,0.25), vec3(0.18,0.35,0.65), band);\n  gl_FragColor = vec4(base, 1.0);\n}"),
    ("tsparticles", "particle-animation", ["web", "react"], ["particles", "confetti", "background"],
     "A configurable particle-effects engine with framework integrations. Selected MIT preset configuration sources are retained without media or runtime bundles.",
     "Use particles as a bounded decorative layer, never behind essential text without an opaque readable surface. Tie celebrations to confirmed success rather than merely clicking submit. Cap particle count, frame rate and duration. Disable pointer interception unless interaction is intentional. Stop and destroy the container on unmount, and select a static fallback for reduced motion. Import only required engine features instead of assuming every bundle is equally small. The original configuration is a planning contract; map fields to your installed engine version.",
     "json", "{\"purpose\":\"confirm a completed milestone\",\"decorative\":true,\"pointerEvents\":\"none\",\"maxParticles\":36,\"maxDurationMs\":900,\"maxFps\":30,\"palette\":[\"#3153c7\",\"#287c66\",\"#ad6b19\"],\"reducedMotion\":\"show a static checkmark with success text\",\"cleanup\":\"destroy effect after completion or unmount\"}"),
    ("gltf-transform", "asset-optimization", ["web"], ["gltf", "optimization", "3d-assets"],
     "A TypeScript/JavaScript toolkit for editing and optimizing glTF 2.0 assets. The tool's MIT license does not cover arbitrary processed models.",
     "Make optimization measurable: record original bytes, texture dimensions, mesh counts and visual reference views. Apply one transformation at a time, then compare silhouette, materials, animations and metadata. Keep the original licensed source asset outside the deployment output. Geometry compression needs compatible decoders and can increase startup cost on weak devices. Texture downscaling often matters more than tiny geometry reductions. Verify model and texture rights independently before publishing either source or optimized output. Archived functions are inert reading references.",
     "json", "{\"asset\":\"original-product-model.glb\",\"rights\":\"verify model and every texture before publication\",\"baseline\":{\"bytes\":0,\"drawCalls\":0,\"texturePixels\":0},\"checks\":[\"silhouette front/side\",\"material roughness\",\"animation duration\",\"named nodes\",\"decoder availability\"],\"deliveryTargets\":{\"mobileBytes\":1500000,\"desktopBytes\":4000000},\"retainOriginal\":true}"),
    ("gl-transitions", "shader-transitions", ["web"], ["glsl", "transition", "gpu"],
     "A collection of GPU transition shaders. Two selected MIT transition sources are archived with per-file notices retained.",
     "A transition shader mixes two images according to progress; it is not a complete renderer. Define sampler orientation, color space, aspect ratio, progress clamping and endpoint behavior in the host. Verify that progress zero reproduces the first image and progress one the second. Avoid distortion-heavy transitions for information that must be read during navigation. Respect reduced motion by swapping content immediately or using a simple fade. Retain shader-specific notices and independently verify rights to the input imagery. The independent shader body illustrates an original controlled wipe.",
     "glsl", "// Original transition function; host provides getFromColor/getToColor.\nvec4 originalWipe(vec2 uv, float progress) {\n  float p = clamp(progress, 0.0, 1.0);\n  if (p <= 0.0) return getFromColor(uv);\n  if (p >= 1.0) return getToColor(uv);\n  float blend = 1.0 - smoothstep(p - 0.015, p + 0.015, uv.x);\n  return mix(getFromColor(uv), getToColor(uv), blend);\n}"),
    ("obs-studio", "video-production", ["linux", "windows", "macos"], ["recording", "streaming", "gpl"],
     "Desktop recording and livestreaming software. GPL license evidence is retained, but no application binaries, plugins or large source tree are mirrored.",
     "Record interface demonstrations as a production workflow separate from interactive UI implementation. Prepare a clean scene, redact personal notifications and credentials, choose a readable capture size and verify audio levels. Record a ten-second sample before a long session. Keep a consent and asset-rights ledger for screens, voices and music. GPL software distribution obligations differ from rights in your recorded output; do not label all footage GPL just because OBS captured it. Exact source headers and build dependencies control redistribution scope. No OBS build is distributed here.",
     "text", "Original recording preflight:\n- Use a demo account with synthetic data.\n- Hide notifications and credential-bearing terminals.\n- Capture at a resolution where UI text remains readable after scaling.\n- Record 10 seconds; check framing, peaks and caption readability.\n- Save a clean master and a delivery copy.\n- Record consent, media licenses and export settings."),
    ("chatterbox", "video-production", ["python"], ["text-to-speech", "voice", "consent"],
     "A text-to-speech project with an MIT code license. No models, audio, cloned voices or dependencies are imported; their rights need separate review.",
     "Treat voice generation as a consent-sensitive production step. Use a voice you control or an explicitly authorized speaker and record permitted purposes, retention and disclosure. Avoid imitating an identifiable person without authorization. Script for speech rather than dense written prose, then verify pronunciation and captions manually. Code licensing does not settle model weights, training provenance or output-use terms. Keep generated narration separate from real identity evidence. This archive provides original workflow guidance and license evidence only.",
     "json", "{\"narrationJob\":\"original-demo\",\"speakerPermission\":{\"status\":\"required\",\"purposes\":[\"product explainer\"],\"impersonationAllowed\":false},\"scriptReview\":[\"factual accuracy\",\"pronunciation\",\"caption parity\"],\"modelsBundled\":false,\"outputReview\":[\"audible artifacts\",\"disclosure\",\"rights scope\"],\"retention\":\"delete unneeded voice inputs\"}"),
    ("tone-js", "browser-audio", ["web"], ["web-audio", "sound", "scheduling"],
     "A Web Audio framework for synthesis and timing. Selected MIT time/frequency source is archived; audio samples are not included.",
     "Audio should be opt-in and should reinforce, not replace, visual feedback. Browsers require a user gesture to start an audio context. Offer mute and level controls, use short envelopes to prevent clicks and keep levels conservative. Schedule audio against the audio clock rather than visual frame timing. Stop active nodes on teardown. A confirmation sound must not be the only success signal. Synthesized tones avoid sample-file licensing but still require thoughtful accessibility and sensory design. The independent example uses native Web Audio.",
     "javascript", "async function originalQuietCue() {\n  const context = new AudioContext();\n  try {\n    await context.resume();\n    const oscillator = context.createOscillator(), gain = context.createGain();\n    const now = context.currentTime;\n    oscillator.frequency.value = 520;\n    gain.gain.setValueAtTime(0, now);\n    gain.gain.linearRampToValueAtTime(0.025, now + 0.01);\n    gain.gain.linearRampToValueAtTime(0, now + 0.12);\n    oscillator.connect(gain); gain.connect(context.destination);\n    oscillator.start(now); oscillator.stop(now + 0.13);\n    oscillator.onended = () => context.close();\n  } catch (error) { await context.close(); throw error; }\n}\n// Invoke only after a user gesture and explicit sound opt-in."),
    ("faster-whisper", "video-production", ["python"], ["transcription", "captions", "speech"],
     "A CTranslate2-based Whisper transcription implementation under MIT. Models, audio and large dependencies are deliberately excluded.",
     "Use transcription to draft captions, not as an unquestioned record. Obtain permission to process the audio, minimize retention and review proper nouns, numbers, speaker changes and timing. Keep captions readable with short lines and sufficient display time. Review model-weight licenses independently of application code. Production throughput depends on model size, hardware, precision and batching; benchmark with representative authorized audio. Generated text can contain mistakes or hallucinations during silence. The original formatter demonstrates deterministic caption serialization without importing the model.",
     "python", "def original_srt_timestamp(seconds):\n    milliseconds = max(0, round(seconds * 1000))\n    hours, milliseconds = divmod(milliseconds, 3600000)\n    minutes, milliseconds = divmod(milliseconds, 60000)\n    whole_seconds, milliseconds = divmod(milliseconds, 1000)\n    return f'{hours:02}:{minutes:02}:{whole_seconds:02},{milliseconds:03}'\n# Review caption text and timing against authorized source audio."),
    ("opencut", "video-production", ["web"], ["editor", "timeline", "media"],
     "An open-source timeline video editor with an MIT root license. No large application tree, user media or bundled dependencies are imported.",
     "Separate the editor's license from the footage, music, fonts and plugins used in a project. Plan an original edit with a clear message and minimal transitions. Keep audio, captions and visual timing on separate tracks, and preserve a high-quality master before delivery compression. Test export duration, aspect ratio, clipping and text legibility. Browser editors may have memory and codec constraints; begin with a short representative project. This entry archives license evidence and an independently written edit decision list, not editor source or assets.",
     "json", "{\"fps\":30,\"canvas\":[1280,720],\"tracks\":[{\"type\":\"visual\",\"clips\":[{\"source\":\"own-title-card\",\"in\":0,\"out\":90},{\"source\":\"own-demo-capture\",\"in\":90,\"out\":300}]},{\"type\":\"captions\",\"clips\":[{\"in\":0,\"out\":90,\"text\":\"Make the handoff explicit\"}]}],\"mediaRights\":\"verify each input\",\"exportChecks\":[\"duration\",\"caption margins\",\"audio peaks\",\"final frame\"]}"),
    ("ffmpeg", "video-production", ["linux", "windows", "macos"], ["encoding", "codec", "lgpl", "gpl"],
     "A multimedia encoding and processing toolkit. Licensing depends on exact build options: default LGPL scope, optional GPL components and possible nonfree combinations.",
     "Use FFmpeg for reproducible media delivery, not as an interactive animation engine. Inspect the installed build configuration before distributing binaries. Optional encoders, linked libraries and nonfree options can change obligations; no universal MIT or LGPL claim applies to every build. Keep input rights independent of tool licensing. Validate frame rate, duration, pixel format, captions and audio after encoding. Never run a command copied from untrusted media metadata. The original command is an illustrative local operation on your own input; the archive does not execute it or distribute FFmpeg.",
     "shell", "# Original illustrative command; review installed codecs and input rights first.\nffmpeg -i own-master.mov -vf 'scale=1280:-2' -c:v libx264 -crf 22 -pix_fmt yuv420p -c:a aac -b:a 128k own-delivery.mp4\n# libx264-enabled builds have GPL implications; this is not a binary redistribution grant.\nffprobe -v error -show_entries format=duration,size -of json own-delivery.mp4"),
]

COMMON = (
    "\n\nOffline implementation checklist: define a useful static state first; test keyboard or native "
    "semantics, high text scaling, narrow layouts and reduced motion; profile on a weak target device; "
    "give every ongoing effect a pause/cleanup path. Separate decorative visuals from essential content. "
    "Inline examples are original illustrative fragments, not upstream API guarantees or bundled runtimes. "
    "Adapt imports, identifiers and accessibility labels to your application. Do not execute archived "
    "HTML or source blindly. Upstream dependencies, fonts, models, voices, samples and stock media "
    "are excluded unless individually recorded in the import report."
)

ADAPTATIONS = {
    "60fps": {
        "language": "dart",
        "description": "Original Flutter state animation. Motion is decorative; state and semantics update immediately.",
        "code": "Widget originalSelection(bool selected, Widget child, BuildContext context) {\n  final reduced = MediaQuery.disableAnimationsOf(context);\n  return Semantics(selected: selected, child: AnimatedContainer(\n    duration: reduced ? Duration.zero : const Duration(milliseconds: 160),\n    curve: Curves.easeOut,\n    padding: const EdgeInsets.all(16),\n    decoration: BoxDecoration(\n      color: selected ? const Color(0xffe8edff) : const Color(0xffffffff),\n      border: Border.all(color: selected ? const Color(0xff3153c7) : const Color(0xffb8c0cf)),\n      borderRadius: BorderRadius.circular(12),\n    ), child: child,\n  ));\n}",
    },
    "refero": {
        "language": "dart",
        "description": "Original Flutter labeled input. Requires Material imports and a caller-owned controller.",
        "code": "Widget originalWorkspaceField(TextEditingController controller) => TextFormField(\n  controller: controller,\n  maxLength: 60,\n  decoration: const InputDecoration(\n    labelText: 'Workspace name',\n    helperText: 'Use a name your teammates recognize.',\n  ),\n  validator: (value) => (value == null || value.trim().isEmpty)\n    ? 'Enter a workspace name.' : null,\n);\n// Keep the controller in caller state and dispose it when that state ends.",
    },
    "react-spring": {
        "language": "jsx",
        "description": "Original React Spring adaptation; requires React and @react-spring/web. reducedMotion is caller-supplied.",
        "code": "import { animated, useSpring } from '@react-spring/web';\nfunction OriginalStatus({ complete, reducedMotion }) {\n  const style = useSpring({\n    opacity: complete ? 1 : 0.7,\n    transform: complete ? 'translateY(0px)' : 'translateY(4px)',\n    immediate: reducedMotion,\n    config: { tension: 180, friction: 24 },\n  });\n  return <animated.p role=\"status\" style={style}>\n    {complete ? 'Saved successfully' : 'Waiting for confirmation'}\n  </animated.p>;\n}",
    },
    "recent": {
        "language": "jsx",
        "description": "Original React semantic information card; parallel to the independent Flutter composition.",
        "code": "function OriginalInfoCard({ title, detail, headingId }) {\n  return <article aria-labelledby={headingId} className=\"original-info-card\">\n    <h2 id={headingId}>{title}</h2><p>{detail}</p>\n  </article>;\n}\n// Use a unique headingId per instance; adapt typography with CSS tokens.",
    },
}


def verify_license(name, text):
    checks = {
        "MIT": ["Permission is hereby granted", "THE SOFTWARE IS PROVIDED"],
        "Apache-2.0": ["Apache License", "Version 2.0"],
        "LGPL-2.1": ["GNU LESSER GENERAL PUBLIC LICENSE", "Version 2.1"],
        "Unlicense": ["This is free and unencumbered software released into the public domain", "THE SOFTWARE IS PROVIDED"],
        "GPL-2.0": ["GNU GENERAL PUBLIC LICENSE", "Version 2"],
        "build-dependent LGPL/GPL": ["LGPL", "GPL"],
    }
    if not all(part.lower() in text.lower() for part in checks[name]):
        raise ValueError("license text does not match expected grant: " + name)


def import_repo(slug, repo, name, license_file, examples):
    revision = PINNED[repo]
    if not re.fullmatch("[a-f0-9]{40}", revision):
        raise ValueError("invalid commit")
    base = "https://raw.githubusercontent.com/" + repo + "/" + revision + "/"
    license_bytes = fetch(base + license_file)
    verify_license(name, license_bytes.decode("utf-8"))
    license_path = save(license_bytes, f"licenses/linked/{slug}/{license_file}",
                        base + license_file, revision, name, "license-evidence", license_file)
    # Apache NOTICE obligations: explicitly check the pinned root. A missing NOTICE
    # is recorded as absent, never silently assumed. No recursive source crawl occurs.
    notices = []
    if name == "Apache-2.0":
        try:
            notice = fetch(base + "NOTICE")
            notices.append(save(notice, f"licenses/linked/{slug}/NOTICE", base + "NOTICE",
                                revision, name, "notice", "NOTICE"))
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            notices.append("root NOTICE absent at pinned revision (HTTP 404)")
    assets = []
    for path in examples:
        try:
            payload = fetch(base + path)
            text = payload.decode("utf-8")
            # Stop rather than copying an obviously narrower third-party grant.
            if re.search(r"creative commons|CC-BY|proprietary", text, re.I):
                raise ValueError("additional/restrictive notice requires manual review")
            relative = save(payload, f"assets/linked/{slug}/{path}", base + path, revision,
                            name, "upstream-source-reference", path)
            wrapper = {
                "version": 1, "kind": "inert-licensed-source-reference", "text": text,
                "source_url": base + path, "revision": revision, "source_path": path,
                "source_sha256": hashlib.sha256(payload).hexdigest(),
                "original_local_path": relative, "license": name, "license_path": license_path,
                "notice": "Source text for offline reading only. No code or HTML is executed or rendered.",
            }
            wrapped = (json.dumps(wrapper, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            if len(wrapped) > FILE_CAP:
                raise ValueError("JSON wrapper byte budget exceeded")
            wrapper_path = save(wrapped, relative + ".json", base + path, revision,
                                name, "inert-source-json-wrapper", path)
            files[-1]["modified"] = True
            files[-1]["transformation"] = "Original source bytes retained separately; UTF-8 text placed in a JSON string with provenance. Source implementation unmodified."
            assets.append({"id": slug + "-source-" + str(len(assets) + 1), "path": wrapper_path,
                           "mime_type": "application/json", "title": "Pinned upstream reference: " + Path(path).name})
        except Exception as error:
            gaps.append({"entry_id": slug, "source_path": path, "reason": str(error)})
    return license_path, assets, {"entry_id": slug, "repository": repo, "revision": revision,
                                 "license": name, "license_path": license_path,
                                 "notice_review": notices, "source_files": len(assets)}


def main():
    discovery_bytes = (ROOT / "discovery.json").read_bytes()
    discovery = json.loads(discovery_bytes)
    links = {}
    provenance = {}
    excluded = {"https://useanimations.com/", "https://github.com/useAnimations/react-useanimations"}
    for source in discovery["sources"]:
        for link in source.get("links", []):
            url = link["url"]
            links.setdefault(url, link)
            provenance.setdefault(url, []).append(source["url"])
    destinations = [link for url, link in links.items() if url not in excluded]
    if len(destinations) != len(KNOWLEDGE):
        raise ValueError("canonical coverage changed; review the manual records")
    repositories = []
    entries = []
    for link, item in zip(destinations, KNOWLEDGE):
        slug, category, platforms, tags, synopsis, guidance, language, code = item
        entry = {
            "id": "linked-" + slug, "title": link["title"], "summary": synopsis,
            "categories": [category, "linked-reference", "original-design-guidance"],
            "tags": tags + ["offline", "original-example"], "platforms": platforms,
            "guidance": guidance + COMMON, "source_url": link["url"],
            "license": "Upstream: " + link["license"] + " Original inline guidance/examples: CC0-1.0.",
            "assets": [], "examples": [{"language": language, "code": code,
                                       "description": "Original illustrative example; not copied upstream. Review integration requirements before use."}],
        }
        if slug in ADAPTATIONS:
            entry["examples"].append(ADAPTATIONS[slug])
        selection = SELECTIONS.get(slug) or ((*NO_SOURCE[slug], []) if slug in NO_SOURCE else None)
        if selection:
            try:
                lp, assets, review = import_repo(slug, *selection)
                entry["license_path"] = lp
                entry["assets"] = assets
                repositories.append(review)
                entry["license"] = (
                    f"Archived upstream selection: {selection[1]}; actual license retained at {lp}. "
                    "Original inline guidance/examples: CC0-1.0. Dependencies/media are not included."
                )
                if slug == "p5-js":
                    entry["license"] += " LGPL source-distribution obligations apply; these unmodified reference files are provided in source form."
                if slug == "ffmpeg":
                    entry["license"] += " Exact builds may be LGPL/GPL/nonfree; no binary redistribution grant is asserted."
                if slug == "obs-studio":
                    entry["license"] += " GPL source/binary distribution obligations depend on exact build; no binary or implementation is included."
            except Exception as error:
                gaps.append({"entry_id": slug, "reason": "license import: " + str(error)})
        else:
            entry["tags"].append("metadata-only-no-redistribution")
            entry["license"] += " Metadata only; no upstream articles, code, screenshot, template, prompt or media redistribution."
        if "license_path" in entry:
            entry["license"] = (
                f"Upstream selection: {selection[1]}; retained license applies. "
                "Original inline guidance/examples: CC0-1.0. No dependencies/media or binaries bundled."
            )
        elif slug == "remotion":
            entry["license"] = (
                "Remotion custom commercial eligibility license, NOT MIT. "
                "Metadata only/no upstream redistribution. Original guidance/examples: CC0-1.0."
            )
        else:
            entry["license"] = (
                "Metadata only/no upstream redistribution; proprietary or unverified content rights. "
                "No screenshots, prompts, templates, articles or media copied. Original guidance/examples: CC0-1.0."
            )
        entries.append(entry)
    library = {"version": 1, "entries": entries}
    (ROOT / "linked-library.json").write_text(json.dumps(library, indent=2, ensure_ascii=False) + "\n")
    report = {
        "version": 1, "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "discovery_manifest": "discovery.json",
        "discovery_sha256": hashlib.sha256(discovery_bytes).hexdigest(),
        "policy": {
            "file_cap_bytes": FILE_CAP, "aggregate_cap_bytes": TOTAL_CAP,
            "network": "Public HTTPS API/raw allowlist only; DNS checked for public addresses on every request/redirect.",
            "selection": "Explicit files only, immutable commit URLs, no archives/clones, no upstream execution.",
            "scope": "Original summaries/guidance/examples; actual upstream license text and small source-only selections.",
            "not_a_mirror": True,
            "original_content": "Original inline examples and editorial guidance dedicated under CC0-1.0; upstream files retain their recorded licenses.",
            "builder_path": "assets/linked/import-builder.py",
            "builder_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "coverage": {
            "canonical_unique_urls": len(links), "linked_destinations": len(destinations),
            "entries": len(entries), "primary_library_targets_excluded": sorted(excluded),
            "source_links": [{"entry_id": e["id"], "url": e["source_url"],
                              "discovered_in": provenance[e["source_url"]],
                              "discovery_license_assessment": links[e["source_url"]]["license"],
                              "discovery_license_url": links[e["source_url"]].get("license_url")} for e in entries],
        },
        "repositories": repositories, "files": files, "gaps": gaps,
        "constraints": [
            "Restrictive galleries: factual metadata only; no screenshot, article, template, proprietary prompt or skill copied.",
            "Remotion: custom commercial eligibility license, NOT MIT; no Remotion software redistributed.",
            "OBS/FFmpeg: license evidence only; no GPL/LGPL binary or large implementation imported.",
            "Chatterbox/faster-whisper/OpenCut: license evidence only; no weights, audio, dependencies or application mirror.",
            "p5: small unmodified LGPL source references, actual license retained; not a MIT relicense.",
            "OGL: actual README license evidence retained; no invented LICENSE file.",
            "Archived source snippets can refer to unavailable dependencies; never claimed to be standalone executable examples.",
        ],
        "counts": {"downloaded_files": len([f for f in files if f["kind"] != "inert-source-json-wrapper"]),
                   "persisted_payload_files": len(files),
                   "retained_license_documents": len([f for f in files if f["kind"] == "license-evidence"]),
                   "archived_bytes": sum(f["bytes"] for f in files),
                   "network_bytes_including_api": transferred,
                   "upstream_source_references": sum(len(e["assets"]) for e in entries),
                   "inert_source_json_wrappers": len([f for f in files if f["kind"] == "inert-source-json-wrapper"]),
                   "original_inline_examples": sum(len(e["examples"]) for e in entries)},
    }
    (ROOT / "linked-import-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"entries": len(entries), **report["counts"], "gaps": gaps}, indent=2))


def validate():
    library = json.loads((ROOT / "linked-library.json").read_text())
    report = json.loads((ROOT / "linked-import-report.json").read_text())
    discovery_bytes = (ROOT / "discovery.json").read_bytes()
    discovery = json.loads(discovery_bytes)
    canonical = {l["url"] for s in discovery["sources"] for l in s.get("links", [])}
    excluded = set(report["coverage"]["primary_library_targets_excluded"])
    assert {e["source_url"] for e in library["entries"]} == canonical - excluded
    assert len(library["entries"]) == 33
    assert report["discovery_sha256"] == hashlib.sha256(discovery_bytes).hexdigest()
    assert report["policy"]["builder_sha256"] == hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    assert not report["gaps"]
    for item in report["files"]:
        parts = Path(item["path"]).parts
        assert parts[:2] in (("assets", "linked"), ("licenses", "linked"))
        assert ".." not in parts and not Path(item["path"]).is_absolute()
        payload = (ROOT / item["path"]).read_bytes()
        assert len(payload) == item["bytes"] <= FILE_CAP
        assert hashlib.sha256(payload).hexdigest() == item["sha256"]
        assert item["revision"] in item["source_url"]
        if item["kind"] == "inert-source-json-wrapper":
            wrapped = json.loads(payload)
            original = (ROOT / wrapped["original_local_path"]).read_bytes()
            assert wrapped["text"].encode("utf-8") == original
            assert hashlib.sha256(original).hexdigest() == wrapped["source_sha256"]
            assert (ROOT / wrapped["license_path"]).is_file()
    assert sum(f["bytes"] for f in report["files"]) == report["counts"]["archived_bytes"] < TOTAL_CAP
    # Exercise the actual offline loader, source-asset retrieval and platform search.
    from designer_mcp.library import Library
    instance = Library(Path(".").resolve())
    merged = instance.catalog().entries
    ids = {e.id for e in merged}
    assert {e["id"] for e in library["entries"]} <= ids
    for entry in library["entries"]:
        for asset in entry["assets"]:
            metadata, binary = instance.asset(entry["id"], asset["id"])
            assert binary is None
            assert metadata
    for query, platform in [("spring", "react"), ("Flutter", "flutter"), ("video", "web")]:
        result = instance.search(query, platform=platform, limit=5)
        assert result
    print(json.dumps({
        "validation": "PASS", "linked_entries": len(library["entries"]),
        "combined_entries": len(merged), "checked_files": len(report["files"]),
        "retrieved_source_assets": report["counts"]["upstream_source_references"],
        "original_examples": report["counts"]["original_inline_examples"],
        "archived_bytes": report["counts"]["archived_bytes"], "gaps": report["gaps"],
    }, indent=2))


if __name__ == "__main__":
    if sys.argv[1:] == ["--allow-network"]:
        main()
    elif sys.argv[1:] == ["--validate-only"]:
        validate()
    else:
        raise SystemExit("Use --allow-network to import, or --validate-only for zero-network checks with the project's Python environment.")
