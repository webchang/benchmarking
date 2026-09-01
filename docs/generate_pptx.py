"""Generate docs/BenchmarkingService.pptx — an overview + architecture deck.

Run with the project env plus python-pptx (no need to add it as a project dep):

    uv run --with python-pptx python docs/generate_pptx.py

Produces a 16:9 deck: title, overview + key design decisions, an architecture
diagram (Client / Service / per-cluster Keycloak + Rossoctl + Workload / MLflow / S3),
the two-token auth model, the benchmark catalog + run lifecycle, and the enact/report
boundaries. Content is sourced from docs/SERVICE_DESIGN_DECISIONS.md.
"""

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

# ---- palette ---------------------------------------------------------------
INK = RGBColor(0x1F, 0x2A, 0x37)       # near-black text
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
NAVY = RGBColor(0x1B, 0x3A, 0x5C)      # title band
BLUE = RGBColor(0x2E, 0x6F, 0xB5)      # service
LTBLUE = RGBColor(0xE8, 0xF1, 0xFB)
CLIENT = RGBColor(0x5B, 0x8C, 0x5A)    # client (green)
LTGREEN = RGBColor(0xE9, 0xF3, 0xE8)
KC = RGBColor(0xB5, 0x5A, 0x2E)        # keycloak (rust)
LTORANGE = RGBColor(0xFB, 0xEE, 0xE4)
ROSSO = RGBColor(0x8E, 0x44, 0xAD)     # rossoctl (purple)
LTPURPLE = RGBColor(0xF1, 0xE8, 0xF6)
WORK = RGBColor(0x3D, 0x6E, 0x70)      # workload (teal)
LTTEAL = RGBColor(0xE4, 0xF0, 0xF0)
STORE = RGBColor(0x6B, 0x6B, 0x6B)     # mlflow/s3 (gray)
LTGRAY = RGBColor(0xEE, 0xEF, 0xF1)
BORDER = RGBColor(0xC7, 0xCE, 0xD6)
ACCENT = RGBColor(0xE8, 0x7A, 0x1E)    # arrow accent

prs = Presentation()
prs.slide_width = Emu(12192000)   # 13.333"
prs.slide_height = Emu(6858000)   # 7.5"
BLANK = prs.slide_layouts[6]

SW = prs.slide_width
SH = prs.slide_height


def _set_font(run, size, bold=False, color=INK, italic=False):
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.name = "Calibri"
    run.font.color.rgb = color


def textbox(slide, x, y, w, h, lines, size=14, color=INK, align=PP_ALIGN.LEFT,
            anchor=MSO_ANCHOR.TOP, bold=False):
    """lines: str, or list of (text, size, bold, color[, bullet_level])."""
    tb = slide.shapes.add_textbox(Emu(int(x)), Emu(int(y)), Emu(int(w)), Emu(int(h)))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    if isinstance(lines, str):
        lines = [(lines, size, bold, color)]
    for i, spec in enumerate(lines):
        text, sz, bd, col = spec[0], spec[1], spec[2], spec[3]
        lvl = spec[4] if len(spec) > 4 else 0
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.level = lvl
        p.space_after = Pt(3)
        r = p.add_run()
        r.text = text
        _set_font(r, sz, bd, col)
    return tb


def box(slide, x, y, w, h, text, fill, line=BORDER, font=13, bold=True,
        font_color=INK, shape=MSO_SHAPE.ROUNDED_RECTANGLE, sub=None, sub_color=None):
    sp = slide.shapes.add_shape(shape, Emu(int(x)), Emu(int(y)), Emu(int(w)), Emu(int(h)))
    sp.fill.solid()
    sp.fill.fore_color.rgb = fill
    sp.line.color.rgb = line
    sp.line.width = Pt(1.0)
    sp.shadow.inherit = False
    tf = sp.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.margin_top = Pt(2)
    tf.margin_bottom = Pt(2)
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = text
    _set_font(r, font, bold, font_color)
    if sub:
        p2 = tf.add_paragraph()
        p2.alignment = PP_ALIGN.CENTER
        r2 = p2.add_run()
        r2.text = sub
        _set_font(r2, font - 3, False, sub_color or font_color)
    return sp


def connector(slide, x1, y1, x2, y2, color=ACCENT, width=2.0, dashed=False, arrow=True):
    cn = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Emu(int(x1)), Emu(int(y1)),
                                    Emu(int(x2)), Emu(int(y2)))
    cn.line.color.rgb = color
    cn.line.width = Pt(width)
    ln = cn.line._get_or_add_ln()
    if arrow:
        tail = ln.makeelement(qn("a:tailEnd"), {"type": "triangle", "w": "med", "len": "med"})
        ln.append(tail)
    if dashed:
        dash = ln.makeelement(qn("a:prstDash"), {"val": "dash"})
        ln.append(dash)
    cn.shadow.inherit = False
    return cn


def title_band(slide, title, subtitle=None):
    band = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, SW, Emu(950000))
    band.fill.solid()
    band.fill.fore_color.rgb = NAVY
    band.line.fill.background()
    band.shadow.inherit = False
    tf = band.text_frame
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.margin_left = Pt(28)
    p = tf.paragraphs[0]
    r = p.add_run()
    r.text = title
    _set_font(r, 26, True, WHITE)
    if subtitle:
        p2 = tf.add_paragraph()
        r2 = p2.add_run()
        r2.text = subtitle
        _set_font(r2, 14, False, RGBColor(0xC9, 0xD9, 0xEC))
    return band


IN = 914400  # EMU per inch


def inch(v):
    return int(v * IN)


# ============================================================ SLIDE 1: TITLE
s = prs.slides.add_slide(BLANK)
bg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, SW, SH)
bg.fill.solid(); bg.fill.fore_color.rgb = NAVY; bg.line.fill.background(); bg.shadow.inherit = False
accent = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, inch(4.35), SW, inch(0.10))
accent.fill.solid(); accent.fill.fore_color.rgb = ACCENT; accent.line.fill.background(); accent.shadow.inherit = False
textbox(s, inch(0.9), inch(2.3), inch(11.5), inch(1.4),
        [("Benchmarking Service", 44, True, WHITE)], anchor=MSO_ANCHOR.MIDDLE)
textbox(s, inch(0.9), inch(3.5), inch(11.5), inch(0.8),
        [("Architecture & Design Overview", 22, False, RGBColor(0xC9, 0xD9, 0xEC))])
textbox(s, inch(0.9), inch(4.7), inch(11.5), inch(1.4),
        [("A pure-Python, HTTP-only service that deploys and evaluates agent benchmarks", 16, False, RGBColor(0xD8, 0xE3, 0xF0)),
         ("across multiple cluster-specific Rossoctl / Kagenti instances.", 16, False, RGBColor(0xD8, 0xE3, 0xF0))])

# ================================================ SLIDE 2: OVERVIEW + DECISIONS
s = prs.slides.add_slide(BLANK)
title_band(s, "Overview & Key Design Decisions")

# Left column: What it is
box(s, inch(0.45), inch(1.25), inch(5.9), inch(0.55), "What it is",
    NAVY, NAVY, font=16, font_color=WHITE, shape=MSO_SHAPE.RECTANGLE)
textbox(s, inch(0.5), inch(1.95), inch(5.85), inch(4.9),
        [("Deploys a benchmark's MCP tool + A2A agent, runs the evaluation, reads results, and exports them — all over HTTP.", 14, False, INK),
         ("Objectives", 14, True, BLUE),
         ("• Automate benchmarking lifecycle operations", 13.5, False, INK, 1),
         ("• Cross-user/cluster sharing of benchmark run results", 13.5, False, INK, 1),
         ("• Easy to configure in-cluster & cross-cluster benchmark runs", 13.5, False, INK, 1),
         ("• Asynchronous parallel benchmark runs", 13.5, False, INK, 1),
         ("• Secure, scalable, auditable & resilient", 13.5, False, INK, 1),
         ("3 benchmarks", 14, True, BLUE),
         ("• gsm8k (single-turn) · tau2 (multi-turn, server-side simulator) · appworld", 13.5, False, INK, 1),
         ("Client contract", 14, True, BLUE),
         ("• Point at a hostname, send Authorization: Bearer <caller JWT> — same from kind to prod", 13.5, False, INK, 1)])

# Right column: Key design decisions (the "overview chart")
box(s, inch(6.6), inch(1.25), inch(6.25), inch(0.55), "Key design decisions",
    NAVY, NAVY, font=16, font_color=WHITE, shape=MSO_SHAPE.RECTANGLE)
decisions = [
    ("Pure-Python, HTTPS-only to Rossoctl", "No shell-out: smaller image, no injection surface, structured errors, testable."),
    ("Two-token auth model", "Caller JWT attributes + routes only (never forwarded); Service mints its own benchmarker (ROPC) token to Rossoctl."),
    ("iss is the trust anchor", "The JWT issuer selects the per-instance config — no separate instance argument, so no confused-deputy escape."),
    ("Per-request ROPC login", "Fresh Service token every request — no expiry handling."),
    ("iss-keyed per-instance config", "One file per issuer: Rossoctl URL, benchmarker cred, MLflow/S3, optional Keycloak backchannel + workload route templates."),
    ("Enact only what HTTP allows", "Deploy/run/report/export; workload Secrets + AuthBridge layer-3 are out-of-band → prechecked/rejected, never silently ignored."),
    ("MLflow with Service; S3 in the cloud", "MLflow is co-located with the Service (per-service traces it emits + reads), not in the workload cluster; S3 is an external cloud service — the shared cross-service sink."),
]
y = inch(1.95)
for head, body in decisions:
    b = box(s, inch(6.6), y, inch(6.25), inch(0.66),
            head, LTBLUE, BLUE, font=12.5, bold=True, font_color=INK,
            shape=MSO_SHAPE.ROUNDED_RECTANGLE)
    b.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    b.text_frame.paragraphs[0].alignment = PP_ALIGN.LEFT
    b.text_frame.margin_left = Pt(8)
    p2 = b.text_frame.add_paragraph()
    p2.alignment = PP_ALIGN.LEFT
    r2 = p2.add_run(); r2.text = body
    _set_font(r2, 10.5, False, RGBColor(0x3A, 0x46, 0x54))
    y += inch(0.70)

# ================================================== SLIDE 3: ARCHITECTURE
s = prs.slides.add_slide(BLANK)
title_band(s, "Architecture", "Relations between client, service, cluster-specific Keycloak / Rossoctl / workload, MLflow & S3")

# Client (off-cluster / host)
box(s, inch(0.4), inch(1.30), inch(2.8), inch(0.85),
    "Benchmarking Client", CLIENT, CLIENT, font=13.5, font_color=WHITE,
    sub="off-cluster (host / CI)", sub_color=RGBColor(0xE6, 0xF0, 0xE6))

# Service (center-left) — title anchored top, bullets below (no overlap)
sv = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, inch(0.4), inch(2.45), inch(2.8), inch(1.85))
sv.fill.solid(); sv.fill.fore_color.rgb = BLUE; sv.line.color.rgb = BLUE; sv.shadow.inherit = False
svtf = sv.text_frame; svtf.word_wrap = True; svtf.vertical_anchor = MSO_ANCHOR.TOP
svtf.margin_left = Pt(8); svtf.margin_top = Pt(7)
p = svtf.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
r = p.add_run(); r.text = "Benchmarking Service"; _set_font(r, 13.5, True, WHITE)
for t in ("pure-Python · async", "iss-keyed InstanceRegistry", "per-request ROPC login",
          "deploy / run / report"):
    pp = svtf.add_paragraph(); pp.alignment = PP_ALIGN.LEFT; pp.level = 1
    rr = pp.add_run(); rr.text = "• " + t; _set_font(rr, 10, False, WHITE)

# MLflow + S3 sit on the SERVICE side, NOT inside the per-cluster workload instance:
# MLflow is co-located with the Service (same cluster); S3 is an external cloud service.
# S3 on the left, MLflow on the right (nearer the collector hop that feeds it).
box(s, inch(0.4), inch(4.75), inch(1.35), inch(0.95), "S3", STORE, ACCENT,
    font=13, font_color=WHITE, sub="cloud (external)", sub_color=LTGRAY)
ml = box(s, inch(1.85), inch(4.75), inch(1.35), inch(0.95), "MLflow", STORE, STORE,
         font=13, font_color=WHITE, sub="same cluster as Service", sub_color=LTGRAY)

# Per-cluster instance container (dashed) — stacked shadow implies "many instances"
CX, CY, CW, CH = inch(4.05), inch(1.2), inch(8.75), inch(4.35)
for off, col in ((inch(0.22), RGBColor(0xDD, 0xDD, 0xDD)), (inch(0.11), RGBColor(0xCB, 0xCB, 0xCB))):
    sh = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, CX + off, CY + off, CW, CH)
    sh.fill.solid(); sh.fill.fore_color.rgb = col; sh.line.fill.background(); sh.shadow.inherit = False
cont = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, CX, CY, CW, CH)
cont.fill.solid(); cont.fill.fore_color.rgb = RGBColor(0xFB, 0xFC, 0xFD)
cont.line.color.rgb = NAVY; cont.line.width = Pt(1.5)
cont.line._get_or_add_ln().append(cont.line._get_or_add_ln().makeelement(qn("a:prstDash"), {"val": "dash"}))
cont.shadow.inherit = False
textbox(s, inch(4.2), inch(1.28), inch(8.4), inch(0.4),
        [("Per-cluster instance — selected by JWT  iss   (ykt3 · kind · …)", 12.5, True, NAVY)])

# Inside the cluster container
kc = box(s, inch(4.35), inch(1.95), inch(2.55), inch(1.0),
         "Keycloak", KC, KC, font=13, font_color=WHITE,
         sub="issuer + backchannel", sub_color=LTORANGE)
ro = box(s, inch(4.35), inch(3.2), inch(2.55), inch(1.1),
         "Rossoctl / Kagenti", ROSSO, ROSSO, font=13, font_color=WHITE,
         sub="backend API (server-side ops)", sub_color=LTPURPLE)

# Workload group
wg = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, inch(9.15), inch(1.95), inch(3.45), inch(2.35))
wg.fill.solid(); wg.fill.fore_color.rgb = LTTEAL; wg.line.color.rgb = WORK; wg.line.width = Pt(1.25)
wg.shadow.inherit = False
textbox(s, inch(9.25), inch(2.02), inch(3.25), inch(0.35),
        [("Benchmark Workload", 12, True, WORK)])
box(s, inch(9.35), inch(2.42), inch(3.05), inch(0.8), "MCP tool", WORK, WORK,
    font=12.5, font_color=WHITE, sub="exgentic-mcp-<benchmark>", sub_color=LTTEAL)
box(s, inch(9.35), inch(3.35), inch(3.05), inch(0.8), "A2A agent", WORK, WORK,
    font=12.5, font_color=WHITE, sub="exgentic-a2a-tool_calling-<b>", sub_color=LTTEAL)

# OTEL collector — lives in the workload cluster; forwards the agent's own OTLP spans to MLflow
# (the agent can't auth to MLflow directly). Optional / off by default (workload_otel).
box(s, inch(6.7), inch(4.5), inch(2.7), inch(0.78), "OTEL collector", WORK, WORK,
    font=12.5, font_color=WHITE, sub="forwards agent spans → MLflow", sub_color=LTTEAL)

# ---- connectors (numbered) ----
def dot(cx, cy, n):
    dia = 0.26 if len(str(n)) < 2 else 0.34  # widen for 2-digit numbers so they don't wrap
    d = s.shapes.add_shape(MSO_SHAPE.OVAL, int(cx) - inch(dia / 2), int(cy) - inch(dia / 2), inch(dia), inch(dia))
    d.fill.solid(); d.fill.fore_color.rgb = ACCENT; d.line.color.rgb = WHITE; d.line.width = Pt(1)
    d.shadow.inherit = False
    tf = d.text_frame; tf.word_wrap = False
    tf.margin_left = 0; tf.margin_right = 0; tf.margin_top = 0; tf.margin_bottom = 0
    p = tf.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
    r = p.add_run(); r.text = str(n); _set_font(r, 10.5, True, WHITE)

# 1 Client -> Keycloak (get caller JWT)
connector(s, inch(3.2), inch(1.7), inch(4.35), inch(2.25))
dot(inch(3.72), inch(1.95), 1)
# 2 Client -> Service (bearer)
connector(s, inch(1.8), inch(2.15), inch(1.8), inch(2.45))
dot(inch(2.05), inch(2.30), 2)
# 3 Service -> Keycloak (validate + mint token)
connector(s, inch(3.2), inch(2.75), inch(4.35), inch(2.6))
dot(inch(3.78), inch(2.66), 3)
# 4 Service -> Rossoctl
connector(s, inch(3.2), inch(3.55), inch(4.35), inch(3.65))
dot(inch(3.78), inch(3.58), 4)
# 5 Rossoctl -> Workload (deploys)
connector(s, inch(6.9), inch(3.45), inch(9.15), inch(2.9), color=ROSSO)
dot(inch(8.0), inch(3.1), 5)
# 6 Service -> Workload (run sessions) — long arrow across
connector(s, inch(3.2), inch(4.1), inch(9.15), inch(3.7), color=WORK)
dot(inch(7.7), inch(3.8), 6)
# 7 Service -> MLflow: EMIT the Agent.Session trace (establishes it first, before the workload spans)
connector(s, inch(2.15), inch(4.30), inch(2.15), inch(4.75), color=STORE)
dot(inch(2.15), inch(4.52), 7)
# 8 A2A agent -> OTEL collector -> MLflow (optional workload spans; nest under 7; off by default)
connector(s, inch(9.5), inch(4.15), inch(9.05), inch(4.5), color=WORK)
connector(s, inch(6.7), inch(4.92), inch(3.2), inch(5.12), color=STORE, dashed=True)
dot(inch(4.7), inch(5.0), 8)
# 9 MLflow -> Service: READ back the Agent.Session traces (after the workload spans have landed)
connector(s, inch(2.9), inch(4.75), inch(2.9), inch(4.30), color=STORE)
dot(inch(2.9), inch(4.52), 9)
# 10 Service -> S3 (export) — terminal, after run completion
connector(s, inch(1.1), inch(4.30), inch(1.1), inch(4.75), color=ACCENT)
dot(inch(0.88), inch(4.52), 10)

# Legend — two-column panel below the frame
legend_l = [
    "1  Client logs in to Keycloak (ROPC) → caller JWT",
    "2  Client → Service:  Authorization: Bearer <caller JWT>",
    "3  Service validates JWT (JWKS) + mints its own",
    "      benchmarker token (ROPC, via backchannel)",
    "4  Service → Rossoctl: deploy / list agents + tools",
    "5  Rossoctl creates the MCP tool + A2A agent in-cluster",
]
legend_r = [
    "6  Service runs benchmark sessions directly (MCP / A2A)",
    "7  Service emits the Agent.Session trace → MLflow (first)",
    "8  A2A agent → OTEL collector → MLflow (optional; off by default)",
    "9  Service reads back Agent.Session traces (MLflow)",
    "10  Service exports run.json / report.* (S3)",
]
lb = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, inch(0.4), inch(5.9), inch(12.5), inch(1.45))
lb.fill.solid(); lb.fill.fore_color.rgb = RGBColor(0xF4, 0xF6, 0xF8); lb.line.color.rgb = BORDER
lb.shadow.inherit = False
for col_x, items in ((inch(0.6), legend_l), (inch(6.7), legend_r)):
    ltf = s.shapes.add_textbox(col_x, inch(5.98), inch(6.0), inch(1.3)).text_frame
    ltf.word_wrap = True
    for i, item in enumerate(items):
        p = ltf.paragraphs[0] if i == 0 else ltf.add_paragraph()
        p.space_after = Pt(1)
        r = p.add_run(); r.text = item; _set_font(r, 10.5, False, INK)

# ================================================ SLIDE 4: TWO-TOKEN AUTH
s = prs.slides.add_slide(BLANK)
title_band(s, "The Two-Token Auth Model", "Why the caller's token is never forwarded upstream")

box(s, inch(0.5), inch(1.4), inch(5.9), inch(0.5), "Caller JWT  (inbound)", CLIENT, CLIENT,
    font=15, font_color=WHITE, shape=MSO_SHAPE.RECTANGLE)
textbox(s, inch(0.6), inch(2.0), inch(5.8), inch(3.6),
        [("Presented by the client as Authorization: Bearer.", 13.5, False, INK),
         ("• Used ONLY to attribute (preferred_username) and route (iss → instance)", 13, False, INK, 1),
         ("• Signature validated via the issuer's JWKS", 13, False, INK, 1),
         ("• aud / exp deliberately lenient in the dev/ops context", 13, False, INK, 1),
         ("• NEVER forwarded to Rossoctl", 13, True, KC, 1),
         ("benchmarker is special:", 13.5, True, CLIENT),
         ("• A caller JWT with preferred_username == benchmarker authorizes config ops (GET/PUT /config)", 13, False, INK, 1)])

box(s, inch(6.9), inch(1.4), inch(5.9), inch(0.5), "Service token  (outbound)", BLUE, BLUE,
    font=15, font_color=WHITE, shape=MSO_SHAPE.RECTANGLE)
textbox(s, inch(7.0), inch(2.0), inch(5.8), inch(3.6),
        [("Minted by the Service per request via ROPC (password grant) using the instance's benchmarker credential.", 13.5, False, INK),
         ("• Always fresh → no expiry handling", 13, False, INK, 1),
         ("• Presented to Rossoctl for all cluster-facing ops", 13, False, INK, 1),
         ("• The Service acts under its OWN identity, not the caller's", 13, True, BLUE, 1),
         ("Backchannel split (iss ≠ dialed host):", 13.5, True, BLUE),
         ("• When the iss host is unreachable (in-cluster kind), JWKS + token come from a configured backchannel URL; iss is matched, never dialed", 13, False, INK, 1)])

box(s, inch(1.6), inch(5.7), inch(10.1), inch(1.1),
    "Implication:  at the Rossoctl / cluster layer every action appears as the Service's identity — "
    "so per-user attribution & audit live in the Service, keyed on (iss, preferred_username).",
    LTBLUE, BLUE, font=13.5, bold=True, font_color=INK)

# ============================================ SLIDE 5: CATALOG + LIFECYCLE
s = prs.slides.add_slide(BLANK)
title_band(s, "Benchmark Catalog & Run Lifecycle")

box(s, inch(0.45), inch(1.25), inch(5.75), inch(0.5), "Catalog (static registry)", NAVY, NAVY,
    font=15, font_color=WHITE, shape=MSO_SHAPE.RECTANGLE)
for i, (name, desc, col, lt) in enumerate([
    ("gsm8k", "single-turn · needs hf-secret + openai-secret", WORK, LTTEAL),
    ("tau2", "multi-turn · user-simulator LLM runs server-side in the MCP pod · raise timeout", ROSSO, LTPURPLE),
    ("appworld", "declared + deploys; MCP image blocked by its own venv-service timeout", KC, LTORANGE),
]):
    y = inch(1.95) + i * inch(1.15)
    b = box(s, inch(0.45), y, inch(5.75), inch(1.0), name, lt, col, font=15, bold=True, font_color=col)
    b.text_frame.paragraphs[0].alignment = PP_ALIGN.LEFT
    b.text_frame.margin_left = Pt(10)
    p2 = b.text_frame.add_paragraph(); p2.alignment = PP_ALIGN.LEFT
    r2 = p2.add_run(); r2.text = desc; _set_font(r2, 11.5, False, INK)

box(s, inch(6.55), inch(1.25), inch(6.3), inch(0.5), "Lifecycle (REST)", NAVY, NAVY,
    font=15, font_color=WHITE, shape=MSO_SHAPE.RECTANGLE)
steps = [
    ("POST /benchmarks/{n}/deploy", "create MCP tool + A2A agent → 201"),
    ("GET /benchmarks/{n}/status", "poll until tool_ready & agent_ready"),
    ("POST /benchmarks/{n}/runs", "precheck → 202 run_id  (409 not deployed · 424 secret missing)"),
    ("GET …/runs/{id}", "poll pending → running → succeeded / failed"),
    ("GET …/runs/{id}/report", "MLflow records + artifacts  (409 if MLflow unset)"),
    ("download from S3", "run.json · report.* · token_report.* · span_report.* · manifest.json"),
]
y = inch(1.95)
for i, (ep, desc) in enumerate(steps):
    b = box(s, inch(6.9), y, inch(5.95), inch(0.66), ep, LTBLUE, BLUE, font=12.5,
            bold=True, font_color=INK)
    b.text_frame.paragraphs[0].alignment = PP_ALIGN.LEFT
    b.text_frame.margin_left = Pt(8)
    p2 = b.text_frame.add_paragraph(); p2.alignment = PP_ALIGN.LEFT
    r2 = p2.add_run(); r2.text = desc; _set_font(r2, 10.5, False, RGBColor(0x3A, 0x46, 0x54))
    if i < len(steps) - 1:
        connector(s, inch(6.75), y + inch(0.33), inch(6.9), y + inch(0.33) + inch(0.7),
                  color=BORDER, width=1.0, arrow=False)
    y += inch(0.75)
# down arrow spine
connector(s, inch(6.72), inch(2.1), inch(6.72), inch(6.4), color=ACCENT, width=1.5)

# ============================================ SLIDE 6: BOUNDARIES
s = prs.slides.add_slide(BLANK)
title_band(s, "What the Service Can & Cannot Enact", "The HTTP-only boundary, made explicit")

box(s, inch(0.5), inch(1.35), inch(5.9), inch(0.5), "✓  Enacts over HTTP", CLIENT, CLIENT,
    font=15, font_color=WHITE, shape=MSO_SHAPE.RECTANGLE)
textbox(s, inch(0.6), inch(2.0), inch(5.8), inch(4.6),
        [("• Deploy MCP tool + A2A agent (with CPU/mem, image, env)", 13, False, INK, 1),
         ("• Deploy-time model swap (per-experiment agent)", 13, False, INK, 1),
         ("• authbridge_enabled → inject sidecar w/ cluster-default pipeline (layer-2)", 13, False, INK, 1),
         ("• Run benchmark sessions; collect pass/fail + latency", 13, False, INK, 1),
         ("• Emit + read MLflow traces; export to S3", 13, False, INK, 1),
         ("• Service-owned config: MLflow + S3 via PUT /config (benchmarker only)", 13, False, INK, 1)])

box(s, inch(6.9), inch(1.35), inch(5.9), inch(0.5), "✗  Out-of-band (reports, doesn't do)", KC, KC,
    font=15, font_color=WHITE, shape=MSO_SHAPE.RECTANGLE)
textbox(s, inch(7.0), inch(2.0), inch(5.8), inch(4.6),
        [("• Cluster Secrets (hf-secret, openai-secret) — operator provisions;", 13, False, INK, 1),
         ("     run precheck returns 424 naming the missing secret", 12, False, RGBColor(0x3A, 0x46, 0x54), 2),
         ("• AuthBridge layer-3 plugin composition / on_error —", 13, False, INK, 1),
         ("     needs a per-agent ConfigMap kubectl overlay; deploy returns 422", 12, False, RGBColor(0x3A, 0x46, 0x54), 2),
         ("• Any cluster-level API call — Rossoctl does these server-side", 13, False, INK, 1),
         ("Principle:", 13.5, True, KC),
         ("• Never silently ignore an un-enactable request — precheck (424) or reject (422) with an actionable reason.", 13, False, INK, 1)])

box(s, inch(1.6), inch(6.5), inch(10.1), inch(0.75),
    "Cluster-agnostic by construction: works on kind / vanilla k8s / OpenShift; "
    "cross-cluster runs use per-instance route templates + a reachable internal issuer.",
    LTGRAY, STORE, font=12.5, bold=True, font_color=INK)

out = "docs/BenchmarkingService.pptx"
prs.save(out)
print("wrote", out, "with", len(prs.slides._sldIdLst), "slides")
