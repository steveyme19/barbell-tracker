"""
Bar Tracker pod v1 (case + lid), parametric. Fusion 360 script.

Builds a new design with every size as a user parameter (Modify > Change Parameters),
so measured numbers drop straight in and the model rebuilds. See hardware/enclosure.md.

Layout (x = length, USB-C end at x = 0; y = width; z = up, floor on the print bed):
  floor (thick enough for the M3 inserts)
  battery pocket, loaded from the far end through a slot in the end wall
  tray with a window: the ledge the XIAO sits on, plus a fence that holds it square
  XIAO, component side up, USB-C against the near end wall (slot in that wall)
  lid on top: LED thin spot, reset pinhole, 4 M2 screws

Run it: Utilities > Add-Ins > Scripts and Add-Ins > + > pick this pod_v1 folder > Run.
(Fusion wants the folder and the .py to have the same name, plus pod_v1.manifest.)
"""

import traceback

import adsk.core
import adsk.fusion

# ---- user parameters: (name, value, comment) ----
# First guesses from the CAD table in hardware/enclosure.md. Measure, then edit them in
# Fusion (Modify > Change Parameters), not here.
PARAMS = [
    ("xiao_l", "21 mm", "XIAO board length (USB-C end to far end)"),
    ("xiao_w", "17.5 mm", "XIAO board width"),
    ("xiao_t", "1.2 mm", "XIAO board thickness"),
    ("xiao_top", "3.5 mm", "tallest part above the board (USB-C)"),
    ("imu_x", "10.5 mm", "MEASURE: IMU center from the board's USB-end corner, along the length"),
    ("imu_y", "8.75 mm", "MEASURE: IMU center from the same corner, across the width"),
    ("batt_l", "33 mm", "LiPo length incl. protection board"),
    ("batt_w", "20.5 mm", "LiPo width"),
    ("batt_t", "8.3 mm", "LiPo thickness"),
    ("wall", "2 mm", "side walls and the far end wall"),
    ("floor_t", "2 mm", "floor (the doc's `floor`; renamed, floor() is a Fusion function). iface_extra is added"),
    ("lid_t", "2 mm", "lid thickness"),
    ("fit", "0.4 mm", "clearance per side around parts"),
    ("m3_insert_d", "4.0 mm", "M3 heat-set insert hole (check the insert's spec)"),
    ("m2_insert_d", "3.2 mm", "M2 heat-set insert hole (check the insert's spec; ~1.6 for self-tapping)"),
    ("iface_spacing", "20 mm", "the two M3 adapter inserts, centered under the IMU"),
    # adapters (not modeled yet, here so they're ready)
    ("sleeve_d", "50 mm", "bar sleeve diameter (adapter A)"),
    ("shaft_d", "28.5 mm", "bar shaft diameter (adapter B)"),
    ("magnet_d", "16 mm", "pot magnet diameter (adapter A)"),
    ("magnet_h", "4.5 mm", "pot magnet height (adapter A)"),
    # extras the model needs
    ("end_wall", "5 mm", "USB-C end wall: thick enough for 2 lid screws beside the board"),
    ("iface_extra", "3.5 mm", "extra floor so the M3 insert hole leaves 1 mm under the battery"),
    ("m3_insert_h", "4 mm", "M3 insert length (hole depth = this + 0.5)"),
    ("m2_insert_h", "4 mm", "M2 insert hole depth from the rim"),
    ("m2_clear", "2.4 mm", "M2 clearance hole in the lid"),
    ("ledge_t", "1.2 mm", "tray (XIAO ledge) thickness over the battery"),
    ("ledge_w", "1.2 mm", "how far the ledge reaches under the board edge"),
    ("fence_w", "2 mm", "fence behind the board's far end"),
    ("boss", "6 mm", "square lid-screw bosses in the far corners"),
    ("screw_inset", "3 mm", "USB-end lid screws, from the side faces"),
    ("usb_slot_w", "12.5 mm", "USB-C slot width (fits a plug's overmold)"),
    ("usb_slot_h", "7 mm", "USB-C slot height"),
    ("usb_z_off", "1.6 mm", "USB-C center above the board's top face"),
    ("wire_l", "4 mm", "battery wire hole in the tray"),
    ("wire_w", "5 mm", "battery wire hole in the tray"),
    ("led_x", "3.5 mm", "MEASURE: LEDs center from the board's USB-end corner, along"),
    ("led_y", "13.5 mm", "MEASURE: LEDs center, across"),
    ("led_win_l", "5 mm", "LED thin spot size"),
    ("led_win_w", "3.5 mm", "LED thin spot size"),
    ("led_skin", "1 mm", "lid left over the LEDs (light shows through)"),
    ("rst_x", "3.5 mm", "MEASURE: reset button center from the USB-end corner, along"),
    ("rst_y", "4 mm", "MEASURE: reset button center, across"),
    ("rst_d", "1.5 mm", "reset pinhole"),
]

# Derived: computed from the ones above. Don't edit these, edit their inputs.
DERIVED = [
    ("cav_l", "batt_l + 2 * fit", "cavity length"),
    ("cav_w", "batt_w + 2 * fit", "cavity width"),
    ("case_l", "end_wall + cav_l + wall", "outside length"),
    ("case_w", "cav_w + 2 * wall", "outside width"),
    ("z_floor", "floor_t + iface_extra", "top of the floor"),
    ("z_bat", "z_floor + batt_t + fit", "top of the battery pocket"),
    ("z_board", "z_bat + ledge_t", "underside of the XIAO"),
    ("z_rim", "z_board + xiao_t + xiao_top + fit", "case height (lid sits here)"),
    ("board_y0", "(case_w - xiao_w - 2 * fit) / 2", "XIAO pocket, near side"),
    ("board_cy", "case_w / 2", "XIAO center line"),
    ("imu_wx", "end_wall + fit + imu_x", "IMU position in the case"),
    ("imu_wy", "board_y0 + fit + imu_y", "IMU position in the case"),
    ("usb_bot", "z_board + xiao_t + usb_z_off - usb_slot_h / 2", "USB-C slot bottom"),
    ("led_wx", "end_wall + fit + led_x", "LEDs in the case"),
    ("led_wy", "board_y0 + fit + led_y", "LEDs in the case"),
    ("rst_wx", "end_wall + fit + rst_x", "reset button in the case"),
    ("rst_wy", "board_y0 + fit + rst_y", "reset button in the case"),
    ("far_sx", "case_l - wall - boss / 2", "far lid screws"),
]

# The 4 lid screws: (x, y)
SCREWS = [
    ("end_wall / 2", "screw_inset"),
    ("end_wall / 2", "case_w - screw_inset"),
    ("far_sx", "wall + boss / 2"),
    ("far_sx", "case_w - wall - boss / 2"),
]

NEW = adsk.fusion.FeatureOperations.NewBodyFeatureOperation
JOIN = adsk.fusion.FeatureOperations.JoinFeatureOperation
CUT = adsk.fusion.FeatureOperations.CutFeatureOperation


class Builder:
    """Sketch + extrude helpers where every size is a parameter expression."""

    def __init__(self, design):
        self.design = design
        self.units = design.unitsManager
        self.planes = {}                         # one construction plane per (component, height)

    def ev(self, expr):
        """Expression -> internal units (cm), for placing geometry before it's dimensioned."""
        return self.units.evaluateExpression(expr, "mm")

    @staticmethod
    def V(expr):
        return adsk.core.ValueInput.createByString(expr)

    def mag(self, expr):
        """Dimensions are unsigned: give the magnitude expression of a signed offset."""
        return expr if self.ev(expr) >= 0 else f"-({expr})"

    def plane(self, comp, z):
        """A construction plane at height z (expression), parallel to XY."""
        if z == "0":
            return comp.xYConstructionPlane
        key = (comp.name, z)
        if key not in self.planes:
            inp = comp.constructionPlanes.createInput()
            inp.setByOffset(comp.xYConstructionPlane, self.V(z))
            pl = comp.constructionPlanes.add(inp)
            pl.name = f"z = {z}"
            self.planes[key] = pl
        return self.planes[key]

    def sketch(self, comp, z, name):
        sk = comp.sketches.add(self.plane(comp, z))
        sk.name = name
        sk.isComputeDeferred = False
        self.z = self.ev(z)
        # Which sketch axis is world X? (decides horizontal vs vertical dimensions)
        self.x_is_h = abs(sk.xDirection.x) > 0.5
        return sk

    def pt(self, sk, x, y):
        return sk.modelToSketchSpace(adsk.core.Point3D.create(x, y, self.z))

    def orient(self, world_axis):
        O = adsk.fusion.DimensionOrientations
        horiz = (world_axis == "x") == self.x_is_h
        return O.HorizontalDimensionOrientation if horiz else O.VerticalDimensionOrientation

    def locate(self, sk, point, x, y):
        """Dimension a sketch point from the origin: x and y are expressions."""
        X, Y = self.ev(x), self.ev(y)
        dims = sk.sketchDimensions
        if abs(X) < 1e-9 and abs(Y) < 1e-9:
            sk.geometricConstraints.addCoincident(point, sk.originPoint)
            return
        if abs(X) > 1e-9:
            d = dims.addDistanceDimension(sk.originPoint, point, self.orient("x"), self.pt(sk, X / 2, Y - 0.4))
            d.parameter.expression = self.mag(x)
        if abs(Y) > 1e-9:
            d = dims.addDistanceDimension(sk.originPoint, point, self.orient("y"), self.pt(sk, X - 0.4, Y / 2))
            d.parameter.expression = self.mag(y)

    def rect(self, sk, x0, y0, w, h):
        """Rectangle from corner (x0, y0), size w x h. All expressions, all dimensioned."""
        X0, Y0, W, H = (self.ev(e) for e in (x0, y0, w, h))
        lines = sk.sketchCurves.sketchLines.addTwoPointRectangle(self.pt(sk, X0, Y0), self.pt(sk, X0 + W, Y0 + H))
        C = adsk.fusion
        for ln in lines:                                     # keep it a rectangle
            kinds = [c.objectType for c in ln.geometricConstraints]
            if C.HorizontalConstraint.classType() in kinds or C.VerticalConstraint.classType() in kinds:
                continue
            g = ln.worldGeometry
            same_y = abs(g.startPoint.y - g.endPoint.y) < 1e-9
            horiz = same_y == self.x_is_h
            (sk.geometricConstraints.addHorizontal if horiz else sk.geometricConstraints.addVertical)(ln)

        def near(p, x, y):
            g = p.worldGeometry
            return abs(g.x - x) < 1e-6 and abs(g.y - y) < 1e-6

        pts = {id(p): p for ln in lines for p in (ln.startSketchPoint, ln.endSketchPoint)}.values()
        corner = next(p for p in pts if near(p, X0, Y0))
        along_x = next(ln for ln in lines if {near(ln.startSketchPoint, X0, Y0), near(ln.endSketchPoint, X0 + W, Y0)} == {True}
                       or {near(ln.endSketchPoint, X0, Y0), near(ln.startSketchPoint, X0 + W, Y0)} == {True})
        along_y = next(ln for ln in lines if {near(ln.startSketchPoint, X0, Y0), near(ln.endSketchPoint, X0, Y0 + H)} == {True}
                       or {near(ln.endSketchPoint, X0, Y0), near(ln.startSketchPoint, X0, Y0 + H)} == {True})
        A = adsk.fusion.DimensionOrientations.AlignedDimensionOrientation
        dims = sk.sketchDimensions
        d = dims.addDistanceDimension(along_x.startSketchPoint, along_x.endSketchPoint, A, self.pt(sk, X0 + W / 2, Y0 - 0.3))
        d.parameter.expression = w
        d = dims.addDistanceDimension(along_y.startSketchPoint, along_y.endSketchPoint, A, self.pt(sk, X0 - 0.3, Y0 + H / 2))
        d.parameter.expression = h
        self.locate(sk, corner, x0, y0)

    def circle(self, sk, cx, cy, d):
        X, Y, D = self.ev(cx), self.ev(cy), self.ev(d)
        c = sk.sketchCurves.sketchCircles.addByCenterRadius(self.pt(sk, X, Y), D / 2)
        dim = sk.sketchDimensions.addDiameterDimension(c, self.pt(sk, X + D, Y + D))
        dim.parameter.expression = d
        self.locate(sk, c.centerSketchPoint, cx, cy)

    def extrude(self, comp, sk, dist, op, body=None, down=False, name=""):
        """Extrude every profile in the sketch by dist (expression), up or down."""
        profs = adsk.core.ObjectCollection.create()
        for p in sk.profiles:
            profs.add(p)
        ext = comp.features.extrudeFeatures
        inp = ext.createInput(profs, op)
        D = adsk.fusion.ExtentDirections
        inp.setOneSideExtent(adsk.fusion.DistanceExtentDefinition.create(self.V(dist)),
                             D.NegativeExtentDirection if down else D.PositiveExtentDirection)
        if body is not None:
            inp.participantBodies = [body]
        f = ext.add(inp)
        if name:
            f.name = name
        return f


def add_params(design):
    up = design.userParameters
    for name, value, comment in PARAMS + DERIVED:
        up.add(name, adsk.core.ValueInput.createByString(value), "mm", comment)


def build_case(b, comp):
    sk = b.sketch(comp, "0", "Outline")
    b.rect(sk, "0", "0", "case_l", "case_w")
    body = b.extrude(comp, sk, "z_rim", NEW, name="Case block").bodies.item(0)
    body.name = "Case"

    sk = b.sketch(comp, "z_floor", "Cavity")
    b.rect(sk, "end_wall", "wall", "cav_l", "cav_w")
    b.extrude(comp, sk, "z_rim - z_floor + 1 mm", CUT, body, name="Cavity")

    # Tray over the battery = the ledge the XIAO sits on
    sk = b.sketch(comp, "z_bat", "Tray")
    b.rect(sk, "end_wall", "wall", "cav_l", "cav_w")
    b.extrude(comp, sk, "ledge_t", JOIN, body, name="Tray")

    # Fence around the XIAO, one board-thickness tall, then the board pocket out of it
    sk = b.sketch(comp, "z_board", "Fence")
    b.rect(sk, "end_wall", "wall", "xiao_l + 2 * fit + fence_w", "cav_w")
    b.extrude(comp, sk, "xiao_t", JOIN, body, name="Fence")
    sk = b.sketch(comp, "z_board", "XIAO pocket")
    b.rect(sk, "end_wall", "board_y0", "xiao_l + 2 * fit", "xiao_w + 2 * fit")
    b.extrude(comp, sk, "xiao_t + 1 mm", CUT, body, name="XIAO pocket")

    # Window under the board: leaves a ledge_w ledge on every side, room for the
    # battery pads and wires underneath
    sk = b.sketch(comp, "z_bat", "Window under XIAO")
    b.rect(sk, "end_wall + ledge_w", "board_y0 + ledge_w", "xiao_l + 2 * fit - 2 * ledge_w", "xiao_w + 2 * fit - 2 * ledge_w")
    b.extrude(comp, sk, "ledge_t + 1 mm", CUT, body, name="Window under XIAO")

    # Battery wires up to the JST socket, past the fence
    sk = b.sketch(comp, "z_bat", "Wire hole")
    b.rect(sk, "end_wall + xiao_l + 2 * fit + fence_w + 0.5 mm", "board_cy - wire_w / 2", "wire_l", "wire_w")
    b.extrude(comp, sk, "ledge_t + 1 mm", CUT, body, name="Wire hole")

    # Far lid-screw bosses, standing on the tray
    sk = b.sketch(comp, "z_board", "Far screw bosses")
    b.rect(sk, "case_l - wall - boss", "wall", "boss", "boss")
    b.rect(sk, "case_l - wall - boss", "case_w - wall - boss", "boss", "boss")
    b.extrude(comp, sk, "z_rim - z_board", JOIN, body, name="Far screw bosses")

    # Battery slot through the far end wall (the battery slides in under the tray)
    sk = b.sketch(comp, "z_floor", "Battery slot")
    b.rect(sk, "case_l - wall - 0.5 mm", "wall", "wall + 1.5 mm", "cav_w")
    b.extrude(comp, sk, "batt_t + fit", CUT, body, name="Battery slot")

    # USB-C slot through the near end wall, open at the top (the lid closes it)
    sk = b.sketch(comp, "usb_bot", "USB-C slot")
    b.rect(sk, "-1 mm", "board_cy - usb_slot_w / 2", "end_wall + 1 mm", "usb_slot_w")   # stops at the inner face
    b.extrude(comp, sk, "z_rim - usb_bot + 1 mm", CUT, body, name="USB-C slot")

    # 2 x M3 inserts from below, iface_spacing apart, centered under the IMU
    sk = b.sketch(comp, "0", "M3 inserts")
    b.circle(sk, "imu_wx - iface_spacing / 2", "imu_wy", "m3_insert_d")
    b.circle(sk, "imu_wx + iface_spacing / 2", "imu_wy", "m3_insert_d")
    b.extrude(comp, sk, "m3_insert_h + 0.5 mm", CUT, body, name="M3 inserts")

    # 4 x M2 lid inserts, down from the rim
    sk = b.sketch(comp, "z_rim", "M2 lid inserts")
    for x, y in SCREWS:
        b.circle(sk, x, y, "m2_insert_d")
    b.extrude(comp, sk, "m2_insert_h", CUT, body, down=True, name="M2 lid inserts")
    return body


def build_lid(b, comp):
    sk = b.sketch(comp, "z_rim", "Lid outline")
    b.rect(sk, "0", "0", "case_l", "case_w")
    body = b.extrude(comp, sk, "lid_t", NEW, name="Lid plate").bodies.item(0)
    body.name = "Lid"

    sk = b.sketch(comp, "z_rim", "Lid screw holes")
    for x, y in SCREWS:
        b.circle(sk, x, y, "m2_clear")
    b.extrude(comp, sk, "lid_t + 1 mm", CUT, body, name="Lid screw holes")

    # LED thin spot: cut from underneath, leaves led_skin of lid over the LEDs
    sk = b.sketch(comp, "z_rim", "LED window")
    b.rect(sk, "led_wx - led_win_l / 2", "led_wy - led_win_w / 2", "led_win_l", "led_win_w")
    b.extrude(comp, sk, "lid_t - led_skin", CUT, body, name="LED window")

    sk = b.sketch(comp, "z_rim", "Reset pinhole")
    b.circle(sk, "rst_wx", "rst_wy", "rst_d")
    b.extrude(comp, sk, "lid_t + 1 mm", CUT, body, name="Reset pinhole")
    return body


def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)   # fresh design
        design = adsk.fusion.Design.cast(app.activeProduct)
        design.designType = adsk.fusion.DesignTypes.ParametricDesignType
        add_params(design)

        root = design.rootComponent
        b = Builder(design)
        I = adsk.core.Matrix3D.create()
        case_occ = root.occurrences.addNewComponent(I)
        case_occ.component.name = "Pod case"
        build_case(b, case_occ.component)
        lid_occ = root.occurrences.addNewComponent(I)
        lid_occ.component.name = "Pod lid"
        build_lid(b, lid_occ.component)

        mm = lambda e: b.ev(e) * 10
        ui.messageBox(
            "Pod v1 built.\n\n"
            f"Outside: {mm('case_l'):.1f} x {mm('case_w'):.1f} x {mm('z_rim + lid_t'):.1f} mm (with lid)\n"
            f"IMU center: x {mm('imu_wx'):.1f}, y {mm('imu_wy'):.1f} mm from the case's USB-end corner\n\n"
            "Change sizes in Modify > Change Parameters. Lines marked MEASURE are guesses.",
            "Bar Tracker pod")
    except Exception:
        if ui:
            ui.messageBox("Pod script failed:\n" + traceback.format_exc())
