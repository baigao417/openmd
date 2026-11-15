# Copyright (c) 2024 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


# Verbatim rect-layout functions; polygon branch not used.
from copy import deepcopy
from typing import Dict,List,Tuple
import numpy as np

def _compute_pairwise_overlap_small(
    coords: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Compute pairwise overlap ratio (mode="small") and bbox areas.

    This only replaces the numeric overlap/area calculation. The caller keeps
    the original sequential dropping order because it affects inline formulas.
    """
    x1 = coords[:, 0]
    y1 = coords[:, 1]
    x2 = coords[:, 2]
    y2 = coords[:, 3]

    areas = np.abs((x2 - x1) * (y2 - y1))

    inter_x1 = np.maximum(x1[:, None], x1[None, :])
    inter_y1 = np.maximum(y1[:, None], y1[None, :])
    inter_x2 = np.minimum(x2[:, None], x2[None, :])
    inter_y2 = np.minimum(y2[:, None], y2[None, :])

    inter_w = np.maximum(0, inter_x2 - inter_x1)
    inter_h = np.maximum(0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h

    small_area = np.minimum(areas[:, None], areas[None, :])
    with np.errstate(divide="ignore", invalid="ignore"):
        overlap = np.where(small_area > 0, inter_area / small_area, 0.0)
    return overlap, areas


def filter_overlap_boxes(
    layout_det_res: Dict[str, List[Dict]], layout_shape_mode: str
) -> Dict[str, List[Dict]]:
    """
    Remove overlapping boxes from layout detection results based on a given overlap ratio.

    Args:
        layout_det_res (Dict[str, List[Dict]]): Layout detection result dict containing a 'boxes' list.

    Returns:
        Dict[str, List[Dict]]: Filtered dict with overlapping boxes removed.
    """
    layout_det_res_filtered = deepcopy(layout_det_res)
    boxes = [
        box for box in layout_det_res_filtered["boxes"] if box["label"] != "reference"
    ]
    if not boxes:
        layout_det_res_filtered["boxes"] = boxes
        return layout_det_res_filtered

    coords = np.array([box["coordinate"] for box in boxes], dtype=np.float64)
    widths = coords[:, 2] - coords[:, 0]
    heights = coords[:, 3] - coords[:, 1]
    overlap_matrix, areas = _compute_pairwise_overlap_small(coords)
    dropped_indexes = set()

    for i in range(len(boxes)):
        if widths[i] < 6 or heights[i] < 6:
            dropped_indexes.add(i)
        for j in range(i + 1, len(boxes)):
            if i in dropped_indexes or j in dropped_indexes:
                continue
            overlap_ratio = overlap_matrix[i, j]
            if (
                boxes[i]["label"] == "inline_formula"
                or boxes[j]["label"] == "inline_formula"
            ):
                if overlap_ratio > 0.5:
                    if boxes[i]["label"] == "inline_formula":
                        dropped_indexes.add(i)
                    if boxes[j]["label"] == "inline_formula":
                        dropped_indexes.add(j)
                    continue
            if overlap_ratio > 0.7:
                if layout_shape_mode != "rect" and "polygon_points" in boxes[i]:
                    poly_overlap_ratio = calculate_polygon_overlap_ratio(
                        boxes[i]["polygon_points"], boxes[j]["polygon_points"], "small"
                    )
                    if poly_overlap_ratio < 0.7:
                        continue
                labels = {boxes[i]["label"], boxes[j]["label"]}
                if labels & {"image", "table", "seal", "chart"} and len(labels) > 1:
                    if "table" not in labels or labels <= {
                        "table",
                        "image",
                        "seal",
                        "chart",
                    }:
                        continue
                if areas[i] >= areas[j]:
                    dropped_indexes.add(j)
                else:
                    dropped_indexes.add(i)
    layout_det_res_filtered["boxes"] = [
        box for idx, box in enumerate(boxes) if idx not in dropped_indexes
    ]
    return layout_det_res_filtered
