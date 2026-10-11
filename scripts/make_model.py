"""Build models/upscale_2x.onnx: a 2x upscaler graph (nearest-neighbour resize).

This is a stand-in with the production interface: NCHW float32 in [0, 1], dynamic
height and width, output at twice the input resolution. Swap in the trained
super-resolution model with the same input and output names and nothing else
changes.

    python scripts/make_model.py [output_path]
"""

import sys
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper


def build(path: Path) -> None:
    image = helper.make_tensor_value_info("image", TensorProto.FLOAT, [1, 3, "height", "width"])
    upscaled = helper.make_tensor_value_info(
        "upscaled", TensorProto.FLOAT, [1, 3, "height_2x", "width_2x"]
    )
    scales = numpy_helper.from_array(np.array([1.0, 1.0, 2.0, 2.0], dtype=np.float32), "scales")
    resize = helper.make_node(
        "Resize",
        inputs=["image", "", "scales"],
        outputs=["upscaled"],
        mode="nearest",
        name="upscale_2x",
    )
    graph = helper.make_graph([resize], "relay_upscale_2x", [image], [upscaled], [scales])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    onnx.checker.check_model(model)
    path.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, path)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "models/upscale_2x.onnx")
    build(target)
    print(f"wrote {target}")
