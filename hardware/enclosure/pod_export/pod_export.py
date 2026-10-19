"""
Export the pod's Case and Lid bodies (from pod_v1) as STL files for printing.

Open the pod design (e.g. v5), then Utilities > Add-Ins > Scripts and Add-Ins >
pod_export > Run. Writes hardware/enclosure/stl/pod_case_raw.stl and pod_lid_raw.stl.
Then `python hardware/enclosure/stl_orient.py` lays them print-side down as pod_case.stl + pod_lid.stl.
"""

import os
import traceback

import adsk.core
import adsk.fusion

# Absolute, so it also works when copied into Fusion's own Scripts folder.
OUT = r"C:\Users\quent\barbell-velocity-tracker\hardware\enclosure\stl"
WANT = {"Case": "pod_case_raw.stl", "Lid": "pod_lid_raw.stl"}


def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        design = adsk.fusion.Design.cast(app.activeProduct)
        os.makedirs(OUT, exist_ok=True)
        done = []
        for comp in design.allComponents:
            for body in comp.bRepBodies:
                if body.name in WANT:
                    path = os.path.join(OUT, WANT[body.name])
                    opt = design.exportManager.createSTLExportOptions(body, path)
                    opt.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh
                    opt.isBinaryFormat = True
                    design.exportManager.execute(opt)
                    done.append(f"{comp.name} / {body.name} -> {path}")
        missing = set(WANT) - {d.split(" / ")[1].split(" ->")[0] for d in done}
        msg = "\n".join(done) or "Nothing exported."
        if missing:
            msg += "\n\nNot found: " + ", ".join(sorted(missing)) + " (is the pod design the active tab?)"
        ui.messageBox(msg, "pod_export")
    except Exception:
        if ui:
            ui.messageBox("Failed:\n" + traceback.format_exc())
