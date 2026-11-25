"""Render a STEP model to a PNG preview, so a part has a picture even if nobody uploaded a photo.

cascadio (OpenCascade, through trimesh) turns the STEP into triangles; matplotlib draws them
headless with simple shading from an isometric-ish angle. A typical part takes well under a second.
"""

import io
import tempfile

import matplotlib

matplotlib.use("Agg")  # headless: no display in the container

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import trimesh  # noqa: E402
from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: E402

STEEL = np.array([0.60, 0.66, 0.74])
LIGHT = np.array([0.4, -0.6, 0.7]) / np.linalg.norm([0.4, -0.6, 0.7])


def render_step_png(step: bytes, width: int = 800, height: int = 600) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".step") as f:
        f.write(step)
        f.flush()
        loaded = trimesh.load(f.name)
    mesh = loaded.to_geometry() if isinstance(loaded, trimesh.Scene) else loaded
    if mesh.is_empty:
        raise ValueError("STEP file has no geometry")

    shade = 0.35 + 0.65 * np.clip(mesh.face_normals @ LIGHT, 0, 1)
    colors = np.c_[np.outer(shade, STEEL), np.ones(len(shade))]
    fig = plt.figure(figsize=(width / 100, height / 100), dpi=100)
    ax = fig.add_subplot(projection="3d")
    # Edges in the face colour hide the hairline seams between triangles.
    ax.add_collection3d(Poly3DCollection(mesh.vertices[mesh.faces], facecolors=colors, edgecolors=colors, linewidths=0.3))
    lo, hi = mesh.bounds
    ax.set(xlim=(lo[0], hi[0]), ylim=(lo[1], hi[1]), zlim=(lo[2], hi[2]))
    ax.set_box_aspect(np.maximum(hi - lo, 1e-6))
    ax.view_init(elev=25, azim=-55)
    ax.set_axis_off()
    fig.subplots_adjust(0, 0, 1, 1)
    out = io.BytesIO()
    fig.savefig(out, format="png", facecolor="white")
    plt.close(fig)
    return out.getvalue()
