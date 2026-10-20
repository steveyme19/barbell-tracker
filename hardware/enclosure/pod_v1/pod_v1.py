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
# Parts measured with calipers 2026-10-19 (hardware/enclosure.md); MEASURE = still a guess.
# To tweak one later, edit it in Fusion (Modify > Change Parameters) or here and rerun.
PARAMS = [
    ("xiao_l", "21 mm", "XIAO board length (USB-C end to far end)"),
    ("xiao_w", "17.8 mm", "XIAO board width"),
    ("xiao_t", "1.0 mm", "XIAO board thickness"),
    ("xiao_top", "3.3 mm", "tallest part above the board (USB-C; radio can is 1.8)"),
    ("imu_x", "8.89 mm", "IMU center from the board's USB-C edge (Seeed KiCad, U3)"),
    ("imu_y", "4.32 mm", "IMU center from the D0-D6 edge, the low-y side (Seeed KiCad, U3)"),
    ("batt_l", "30.5 mm", "LiPo length incl. protection board (PL 602030, 400 mAh)"),
    ("batt_w", "19.7 mm", "LiPo width"),
    ("batt_t", "7.6 mm", "LiPo thickness at the protection board (cell ~6.0)"),
    ("batt_wire_l", "2.5 mm", "room past the protection-board end for the wires to bend up (not measured)"),
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
    ("batt_gap", "2 mm", "space between battery and tray: foam tape, and the 2 battery wires running to the pads under the board"),
    ("ledge_w", "1.2 mm", "how far the ledge reaches under the board edge"),
    ("fence_w", "2 mm", "fence behind the board's far end"),
    ("boss", "5.8 mm", "square lid-screw bosses in the far corners (5.8 so the switch pocket fits between)"),
    ("screw_inset", "3 mm", "USB-end lid screws, from the side faces"),
    ("usb_slot_w", "11.5 mm", "USB-C slot width (measured overmold 11.0)"),
    ("usb_slot_h", "6.6 mm", "USB-C slot height (measured overmold 6.0)"),
    ("usb_z_off", "1.7 mm", "USB-C center above the board's top face (top 3.3, 3.2 tall)"),
    ("wire_l", "4 mm", "battery wire hole in the tray"),
    ("wire_w", "5 mm", "battery wire hole in the tray"),
    ("led_x", "1.7 mm", "LED window center from the USB-C edge: RGB at 2.8, charge LED at 0.6 (Seeed KiCad)"),
    ("led_y", "14.6 mm", "both LEDs from the D0-D6 edge (Seeed KiCad)"),
    ("led_win_l", "5 mm", "LED thin spot size"),
    ("led_win_w", "3.5 mm", "LED thin spot size"),
    ("led_skin", "1 mm", "lid left over the LEDs (light shows through)"),
    ("rst_x", "1.91 mm", "reset button center from the USB-C edge (Seeed KiCad, K1)"),
    ("rst_y", "3.17 mm", "reset button center from the D0-D6 edge (Seeed KiCad, K1)"),
    ("rst_d", "1.5 mm", "reset pinhole"),
    # looks: rounded edges, but every face stays flat (the pod is the calibration cube)
    ("corner_r", "2 mm", "fillet on the 4 vertical corners, case and lid (keep <= wall)"),
    ("bottom_ch", "0.6 mm", "chamfer round the bottom edge (a chamfer prints floor-down, a fillet wouldn't)"),
    ("lid_r", "0.8 mm", "fillet round the lid's top edge (keep < lid_t)"),
    # stand-in parts (for the exploded view and Inspect > Interference)
    ("usb_l", "7.5 mm", "USB-C receptacle length on the board"),
    ("usb_w", "9 mm", "USB-C receptacle width"),
    ("usb_h", "3.2 mm", "USB-C receptacle height"),
    ("usb_over", "1.3 mm", "how far the USB-C sticks out past the board edge"),
    ("imu_s", "2.5 mm", "IMU chip size (square)"),
    # battery door: a plug that fills the end slot flush (that face stays flat)
    ("plug_fit", "0.15 mm", "clearance per side round the lid's end wall (it closes the battery end)"),
    # USB-C: a relief round the slot outside, so a plug's overmold reaches the socket
    ("usb_relief_w", "12 mm", "relief width round the USB-C slot (measured overmold 11.0 + 0.5 per side)"),
    ("usb_wall_min", "1.5 mm", "end wall left where the plug passes, inside the relief"),
    # power switch: SS12D00-type mini slide switch in the battery + lead, lever out the far end
    ("sw_l", "8.6 mm", "MEASURE: switch body length (along the lever travel)"),
    ("sw_d", "3.6 mm", "MEASURE: switch body depth"),
    ("sw_h", "3.5 mm", "MEASURE: switch body height"),
    ("sw_lever", "4 mm", "MEASURE: lever length out of the body"),
    ("sw_slot_w", "4.5 mm", "lever slot width (lever + travel)"),
    ("sw_slot_h", "2 mm", "lever slot height"),
    ("sw_recess", "1 mm", "recess round the lever on the outside, so the lever doesn't stick out"),
    ("sw_wall", "0.8 mm", "cradle walls round the switch"),
]

# Derived: computed from the ones above. Don't edit these, edit their inputs.
DERIVED = [
    ("cav_l", "batt_l + batt_wire_l + 2 * fit", "cavity length"),
    ("cav_w", "batt_w + 2 * fit", "cavity width"),
    ("case_l", "end_wall + cav_l + wall", "outside length"),
    ("case_w", "cav_w + 2 * wall", "outside width"),
    ("z_floor", "floor_t + iface_extra", "top of the floor"),
    ("z_bat", "z_floor + batt_t + fit + batt_gap", "top of the battery pocket (incl. the wire / foam gap)"),
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
    ("usb_top", "usb_bot + usb_slot_h", "top of the USB-C plug clearance"),
    ("usb_z", "z_board + xiao_t + usb_z_off", "USB-C socket center height"),
    ("usb_rel_top", "z_rim + lid_t - 0.6 mm", "relief top: 0.6 mm of lid left over it"),
    ("usb_rel_bot", "2 * usb_z - usb_rel_top", "relief bottom, centered on the socket"),
    ("sw_face", "case_l - sw_recess / 2 - sw_lever", "switch body face (lever tip ends inside the recess)"),
    ("sw_z", "z_board + sw_h / 2", "lever height"),
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
        self.skipped = []                        # dimensions Fusion already had fixed (see dim())

    def dim(self, sk, what, make, expr):
        """Add a driving dimension and set its expression. If Fusion says the sketch is
        already over-constrained, that degree of freedom is fixed by a constraint Fusion
        inferred (e.g. two hole centres at the same height snapped level), so skip it."""
        try:
            d = make()
        except RuntimeError as e:
            if "OVER_CONSTRAINTS" not in str(e):
                raise
            self.skipped.append(f"{sk.name}: {what}")
            return None
        d.parameter.expression = expr
        return d

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
            self.dim(sk, f"x = {x}", lambda: dims.addDistanceDimension(
                sk.originPoint, point, self.orient("x"), self.pt(sk, X / 2, Y - 0.4)), self.mag(x))
        if abs(Y) > 1e-9:
            self.dim(sk, f"y = {y}", lambda: dims.addDistanceDimension(
                sk.originPoint, point, self.orient("y"), self.pt(sk, X - 0.4, Y / 2)), self.mag(y))

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
        self.dim(sk, f"width = {w}", lambda: dims.addDistanceDimension(
            along_x.startSketchPoint, along_x.endSketchPoint, A, self.pt(sk, X0 + W / 2, Y0 - 0.3)), w)
        self.dim(sk, f"height = {h}", lambda: dims.addDistanceDimension(
            along_y.startSketchPoint, along_y.endSketchPoint, A, self.pt(sk, X0 - 0.3, Y0 + H / 2)), h)
        self.locate(sk, corner, x0, y0)

    def circle(self, sk, cx, cy, d):
        X, Y, D = self.ev(cx), self.ev(cy), self.ev(d)
        c = sk.sketchCurves.sketchCircles.addByCenterRadius(self.pt(sk, X, Y), D / 2)
        self.dim(sk, f"diameter = {d}", lambda: sk.sketchDimensions.addDiameterDimension(c, self.pt(sk, X + D, Y + D)), d)
        self.locate(sk, c.centerSketchPoint, cx, cy)

    # ---- edge finishing (fillets / chamfers). Edges are picked by where they are. ----

    @staticmethod
    def edges(body, pick):
        col = adsk.core.ObjectCollection.create()
        for e in body.edges:
            if e.startVertex is None or e.endVertex is None:
                continue
            if pick(e.startVertex.geometry, e.endVertex.geometry):
                col.add(e)
        return col

    def vertical_edges(self, body):
        return self.edges(body, lambda a, b: abs(a.x - b.x) < 1e-6 and abs(a.y - b.y) < 1e-6)

    def edges_at_z(self, body, z):
        Z = self.ev(z)
        return self.edges(body, lambda a, b: abs(a.z - Z) < 1e-6 and abs(b.z - Z) < 1e-6)

    def fillet(self, comp, edges, r, name):
        fl = comp.features.filletFeatures
        inp = fl.createInput()
        try:                                   # current API
            inp.edgeSetInputs.addConstantRadiusEdgeSet(edges, self.V(r), True)
        except AttributeError:                 # older Fusion
            inp.addConstantRadiusEdgeSet(edges, self.V(r), True)
        f = fl.add(inp)
        f.name = name
        return f

    def chamfer(self, comp, edges, d, name):
        ch = comp.features.chamferFeatures
        try:                                   # current API
            inp = ch.createInput2()
            inp.chamferEdgeSets.addEqualDistanceChamferEdgeSet(edges, self.V(d), True)
        except AttributeError:                 # older Fusion
            inp = ch.createInput(edges, True)
            inp.setToEqualDistance(self.V(d))
        f = ch.add(inp)
        f.name = name
        return f

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
    # Round the corners and break the bottom edge first, while the block is a plain box
    b.fillet(comp, b.vertical_edges(body), "corner_r", "Corner fillets")
    b.chamfer(comp, b.edges_at_z(body, "0"), "bottom_ch", "Bottom chamfer")

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

    # Far end open full height: the battery slides in here, and the lid's end wall closes
    # it, so the 4 lid screws hold the battery in (no loose door).
    sk = b.sketch(comp, "z_floor", "End opening")
    b.rect(sk, "case_l - wall - 0.5 mm", "wall", "wall + 1.5 mm", "cav_w")
    b.extrude(comp, sk, "z_rim - z_floor + 1 mm", CUT, body, name="End opening")

    # USB-C slot through the near end wall, open at the top (the lid closes it)
    sk = b.sketch(comp, "usb_bot", "USB-C slot")
    b.rect(sk, "-1 mm", "board_cy - usb_slot_w / 2", "end_wall + 1 mm", "usb_slot_w")   # stops at the inner face
    b.extrude(comp, sk, "z_rim - usb_bot + 1 mm", CUT, body, name="USB-C slot")
    # Relief outside the slot: only usb_wall_min of wall where the plug passes, so the
    # overmold gets in far enough for the plug to seat (the wall is 5 mm for the screws).
    sk = b.sketch(comp, "usb_rel_bot", "USB-C relief")
    b.rect(sk, "-1 mm", "board_cy - usb_relief_w / 2", "end_wall - usb_wall_min + 1 mm", "usb_relief_w")
    b.extrude(comp, sk, "z_rim - usb_rel_bot + 1 mm", CUT, body, name="USB-C relief")

    # Power switch cradle at the far end, between the bosses: a block with a pocket the
    # switch drops into from the top and a lever slot out of its front. The lever then goes
    # through the lid's end wall (slot + recess there), tip inside the face.
    sk = b.sketch(comp, "z_board", "Switch cradle")
    b.rect(sk, "sw_face - sw_d - sw_wall", "board_cy - sw_l / 2 - sw_wall",
           "case_l - wall - (sw_face - sw_d - sw_wall)", "sw_l + 2 * sw_wall")
    b.extrude(comp, sk, "sw_h", JOIN, body, name="Switch cradle")
    sk = b.sketch(comp, "z_board", "Switch pocket")
    b.rect(sk, "sw_face - sw_d", "board_cy - sw_l / 2", "sw_d", "sw_l")
    b.extrude(comp, sk, "sw_h + 1 mm", CUT, body, name="Switch pocket")
    sk = b.sketch(comp, "sw_z - sw_slot_h / 2", "Lever slot")
    b.rect(sk, "sw_face - 0.01 mm", "board_cy - sw_slot_w / 2", "case_l + 1 mm - (sw_face - 0.01 mm)", "sw_slot_w")
    b.extrude(comp, sk, "z_rim - (sw_z - sw_slot_h / 2) + 1 mm", CUT, body, name="Lever slot")

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
    b.fillet(comp, b.vertical_edges(body), "corner_r", "Lid corner fillets")     # same as the case
    b.fillet(comp, b.edges_at_z(body, "z_rim + lid_t"), "lid_r", "Lid top fillet")

    # End wall hanging from the lid: closes the case's far end, so the screws hold the
    # battery in. Printed lid-down it's just a wall standing on the plate.
    sk = b.sketch(comp, "z_floor + plug_fit", "Lid end wall")
    b.rect(sk, "case_l - wall", "wall + plug_fit", "wall", "cav_w - 2 * plug_fit")
    b.extrude(comp, sk, "z_rim - z_floor - plug_fit", JOIN, body, name="Lid end wall")
    # The switch lever goes through it: a slot open at the bottom (it slides down over the
    # lever as the lid goes on) and a recess outside so the lever tip stays in the face.
    sk = b.sketch(comp, "z_floor", "Lever slot (lid)")
    b.rect(sk, "case_l - wall - 1 mm", "board_cy - sw_slot_w / 2", "wall + 2 mm", "sw_slot_w")
    b.extrude(comp, sk, "sw_z + sw_slot_h / 2 - z_floor", CUT, body, name="Lever slot (lid)")
    sk = b.sketch(comp, "sw_z - sw_slot_h / 2 - 1.5 mm", "Lever recess")
    b.rect(sk, "case_l - sw_recess", "board_cy - sw_slot_w / 2 - 1.5 mm", "sw_recess + 1 mm", "sw_slot_w + 3 mm")
    b.extrude(comp, sk, "sw_slot_h + 3 mm", CUT, body, name="Lever recess")

    sk = b.sketch(comp, "z_rim", "Lid screw holes")
    for x, y in SCREWS:
        b.circle(sk, x, y, "m2_clear")
    b.extrude(comp, sk, "lid_t + 1 mm", CUT, body, name="Lid screw holes")

    # LED thin spot: cut from underneath, leaves led_skin of lid over the LEDs
    sk = b.sketch(comp, "z_rim", "LED window")
    b.rect(sk, "led_wx - led_win_l / 2", "led_wy - led_win_w / 2", "led_win_l", "led_win_w")
    b.extrude(comp, sk, "lid_t - led_skin", CUT, body, name="LED window")

    # The USB-C plug's overmold reaches above the rim: a pocket under the lid at the USB
    # end, open on the end face, so a cable fits with the lid on. The lid top stays solid.
    if b.ev("usb_top - z_rim") > 0:
        sk = b.sketch(comp, "z_rim", "USB-C plug pocket")
        b.rect(sk, "-1 mm", "board_cy - usb_slot_w / 2", "end_wall + 1 mm", "usb_slot_w")
        b.extrude(comp, sk, "usb_top - z_rim", CUT, body, name="USB-C plug pocket")
    # ... and the USB-C relief continues into the lid's underside
    sk = b.sketch(comp, "z_rim", "USB-C relief (lid)")
    b.rect(sk, "-1 mm", "board_cy - usb_relief_w / 2", "end_wall - usb_wall_min + 1 mm", "usb_relief_w")
    b.extrude(comp, sk, "usb_rel_top - z_rim", CUT, body, name="USB-C relief (lid)")

    sk = b.sketch(comp, "z_rim", "Reset pinhole")
    b.circle(sk, "rst_wx", "rst_wy", "rst_d")
    b.extrude(comp, sk, "lid_t + 1 mm", CUT, body, name="Reset pinhole")
    return body


def build_parts(b, root, I):
    """Stand-ins for the electronics, each its own component, placed where they sit in the
    case. For the exploded view, and Inspect > Interference shows if a real size won't fit."""
    xiao = root.occurrences.addNewComponent(I).component
    xiao.name = "XIAO (stand-in)"
    sk = b.sketch(xiao, "z_board", "Board")
    b.rect(sk, "end_wall + fit", "board_y0 + fit", "xiao_l", "xiao_w")
    b.extrude(xiao, sk, "xiao_t", NEW, name="Board").bodies.item(0).name = "Board"
    sk = b.sketch(xiao, "z_board + xiao_t", "USB-C")
    b.rect(sk, "end_wall + fit - usb_over", "board_cy - usb_w / 2", "usb_l", "usb_w")
    b.extrude(xiao, sk, "usb_h", NEW, name="USB-C").bodies.item(0).name = "USB-C"
    sk = b.sketch(xiao, "z_board + xiao_t", "IMU")
    b.rect(sk, "imu_wx - imu_s / 2", "imu_wy - imu_s / 2", "imu_s", "imu_s")
    b.extrude(xiao, sk, "1 mm", NEW, name="IMU").bodies.item(0).name = "IMU"

    batt = root.occurrences.addNewComponent(I).component
    batt.name = "LiPo (stand-in)"
    sk = b.sketch(batt, "z_floor", "Cell")
    b.rect(sk, "end_wall + fit", "wall + fit", "batt_l", "batt_w")
    b.extrude(batt, sk, "batt_t", NEW, name="Cell").bodies.item(0).name = "Cell"


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
        build_parts(b, root, I)

        mm = lambda e: b.ev(e) * 10
        ui.messageBox(
            "Pod v1 built.\n\n"
            f"Outside: {mm('case_l'):.1f} x {mm('case_w'):.1f} x {mm('z_rim + lid_t'):.1f} mm (with lid)\n"
            f"IMU center: x {mm('imu_wx'):.1f}, y {mm('imu_wy'):.1f} mm from the case's USB-end corner\n\n"
            "Change sizes in Modify > Change Parameters. Lines marked MEASURE are guesses."
            + (("\n\nDimensions Fusion had already fixed (skipped):\n" + "\n".join(b.skipped)) if b.skipped else ""),
            "Bar Tracker pod")
    except Exception:
        if ui:
            ui.messageBox("Pod script failed:\n" + traceback.format_exc())
