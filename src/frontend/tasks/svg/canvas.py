import copy
import math
import time
import uuid
from PyQt6.QtCore import Qt, QEvent, QPointF, QRectF, QTimer, QVariantAnimation, QEasingCurve, pyqtSignal
from PyQt6.QtGui import QColor, QBrush, QCursor, QFont, QKeySequence, QMouseEvent, QPainter, QPainterPath, QPainterPathStroker, QPen, QPixmap, QPolygonF, QTransform, QUndoCommand, QUndoStack
from PyQt6.QtWidgets import QApplication, QGraphicsEllipseItem, QGraphicsPathItem, QGraphicsScene, QGraphicsTextItem, QGraphicsView
from tasks.svg.geometry import StrokeFilter, distance_squared, segment_distance_squared, simplify_points, rounded_polyline, smooth_stroke, point_widths, set_widths, remap_widths, reshape_stroke, stroke_outline


def painter_path(points, width=1, widths=None):
    path = QPainterPath()
    if widths is not None:
        left, right = stroke_outline(points, widths)
        if not right:
            path.addEllipse(QPointF(*points[0]), widths[0] / 2, widths[0] / 2)
            return path
        path.addPolygon(QPolygonF([QPointF(*point) for point in left]))
        for index, side in ((-1, left), (0, right)):
            x, y = points[index]
            radius = widths[index] / 2
            angle = math.degrees(math.atan2(y - side[index][1], side[index][0] - x))
            path.arcTo(QRectF(x - radius, y - radius, radius * 2, radius * 2), angle, 180)
            if index == -1:
                for point in reversed(right[:-1]):
                    path.lineTo(*point)
        path.closeSubpath()
        path.setFillRule(Qt.FillRule.WindingFill)
    elif len(points) == 1:
        path.addEllipse(QPointF(*points[0]), width / 2, width / 2)
    elif points:
        path.addPolygon(QPolygonF([QPointF(*point) for point in points]))
        if len(points) > 2 and points[0] == points[-1]:
            path.closeSubpath()
    return path


class StrokeCommand(QUndoCommand):
    def __init__(self, canvas, index, before, after, text):
        super().__init__(text)
        self.canvas = canvas
        self.changes = [(index, before, after)]

    def redo(self):
        self.canvas.apply_changes(self.changes)

    def undo(self):
        self.canvas.apply_changes([(index, after, before) for index, before, after in self.changes])


class StrokeGroupCommand(StrokeCommand):
    def __init__(self, canvas, changes, text):
        super().__init__(canvas, 0, None, None, text)
        self.changes = changes


class ClearCommand(QUndoCommand):
    def __init__(self, canvas):
        super().__init__("Clear tracing")
        self.canvas = canvas
        self.strokes = list(canvas.strokes)

    def redo(self):
        self.canvas.replace_strokes([])

    def undo(self):
        self.canvas.replace_strokes(self.strokes)


class TracePathItem(QGraphicsPathItem):
    def __init__(self, canvas):
        super().__init__()
        self.clip_rect = QRectF(canvas.image.rect())
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        canvas.graphics_scene.addItem(self)

    def paint(self, painter, option, widget=None):
        painter.save()
        painter.setClipRect(self.clip_rect.translated(-self.pos()), Qt.ClipOperation.IntersectClip)
        super().paint(painter, option, widget)
        painter.restore()


class StrokeItem(TracePathItem):
    def __init__(self, canvas, stroke):
        super().__init__(canvas)
        self.setZValue(1)
        self.set_stroke(stroke)

    def set_stroke(self, stroke):
        self.stroke = stroke
        self.stroke_id = stroke["id"]
        self.setPos(0, 0)
        centerline = painter_path(stroke["points"], stroke.get("width", 1))
        self.setPath(painter_path(stroke["points"], stroke.get("width", 1), stroke["widths"]) if "widths" in stroke else centerline)
        self.point_bounds = (QRectF(QPointF(*stroke["points"][0]), QPointF(*stroke["points"][0]))
                             if len(stroke["points"]) == 1 else centerline.boundingRect())
        stroker = QPainterPathStroker()
        stroker.setWidth(stroke.get("width", 1))
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self.hit_shape = self.path() if len(stroke["points"]) == 1 or "widths" in stroke else stroker.createStroke(self.path())
        self.highlighted = None
        self.highlight(False)

    def shape(self):
        return self.hit_shape

    def highlight(self, selected):
        if selected == self.highlighted:
            return
        self.highlighted = selected
        color = QColor("#00b7ff" if selected else "#ff28b5")
        self.set_color(color)

    def set_color(self, color):
        if len(self.stroke["points"]) == 1 or "widths" in self.stroke:
            self.setPen(QPen(Qt.PenStyle.NoPen))
            self.setBrush(QBrush(color))
        else:
            self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            self.setPen(QPen(color, self.stroke.get("width", 1), Qt.PenStyle.SolidLine,
                             Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))


class NodeHandle(QGraphicsEllipseItem):
    def __init__(self, canvas, index, point):
        super().__init__(-4, -4, 8, 8)
        self.canvas = canvas
        self.index = index
        self.before = None
        self.setPos(*point)
        self.setBrush(QBrush(QColor("white")))
        self.setPen(QPen(QColor("#0078d4")))
        self.setZValue(3)
        self.setFlags(self.GraphicsItemFlag.ItemIsMovable |
                      self.GraphicsItemFlag.ItemSendsGeometryChanges |
                      self.GraphicsItemFlag.ItemIgnoresTransformations)

    def itemChange(self, change, value):
        if change == self.GraphicsItemChange.ItemPositionChange:
            return self.canvas.clamp_point(value)
        if change == self.GraphicsItemChange.ItemPositionHasChanged and self.before is not None:
            self.canvas.move_node(self.index, value)
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        self.before = self.canvas.strokes[self.canvas.selected_index]
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        before = self.before
        self.before = None
        if before is not None:
            self.canvas.commit_node_move(before)


class ImageCanvas(QGraphicsView):
    strokes_changed = pyqtSignal()
    selection_changed = pyqtSignal(int)
    message = pyqtSignal(str)
    view_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.graphics_scene = QGraphicsScene(self)
        self.setScene(self.graphics_scene)
        self.image = None
        self.image_item = None
        self.stroke_items = []
        self.items_by_id = {}
        self.stroke_indices = {}
        self.node_items = []
        self.strokes = []
        self.selected_ids = set()
        self.selected_index = -1
        self.tool = "draw"
        self.show_nodes = False
        self.view_mode = "Overlay"
        self.image_opacity = 0.65
        self.tolerance = 0.35
        self.brush_width = 1.0
        self.eraser_size = 24
        self.smoothing = False
        self.stroke_filter = None
        self.current_points = []
        self.current_widths = []
        self.redo_points = []
        self.polyline_base_index = None
        self.corner_radius = 0.0
        self.current_radius = 0.0
        self.current_width = 1.0
        self.drawing_width = 1.0
        self.continue_index = -1
        self.continue_start = False
        self.preview_items = []
        self.preview_count = 0
        self.start_marker = None
        self.panning = False
        self.space_held = False
        self.pan_binding = QKeySequence("Space")
        self.pan_start = QPointF()
        self.drag_start = None
        self.drag_delta = QPointF()
        self.drag_bounds = QRectF()
        self.rubber_item = None
        self.rubber_start = None
        self.rubber_selection = set()
        self.erasing = False
        self.erase_timer = QTimer(self)
        self.erase_timer.setSingleShot(True)
        self.erase_timer.setInterval(250)
        self.erase_timer.timeout.connect(self.finish_erase)
        self.erased_ids = set()
        self.eraser_point = None
        self.cursor_point = None
        self.hover_endpoint = None
        self.keyboard_drawing = False
        self.node_after = None
        self.undo_stack = QUndoStack(self)
        self.undo_stack.setUndoLimit(200)
        self.zoom_animation = QVariantAnimation(self)
        self.zoom_animation.setDuration(100)
        self.zoom_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.zoom_animation.setStartValue(1.0)
        self.zoom_animation.setEndValue(1.0)
        self.zoom_animation.valueChanged.connect(self.animate_zoom)
        self.stroke_animation = QVariantAnimation(self)
        self.stroke_animation.setDuration(100)
        self.stroke_animation.setStartValue(QColor("#ff28b5"))
        self.stroke_animation.setEndValue(QColor("#00b7ff"))
        self.stroke_animation.valueChanged.connect(self.animate_stroke)
        self.animated_stroke = None
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(8)
        self.preview_timer.timeout.connect(self.update_preview)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(self.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(self.ViewportAnchor.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setTabletTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def reset_scene(self):
        self.erase_timer.stop()
        self.zoom_animation.stop()
        self.stroke_animation.stop()
        self.animated_stroke = None
        self.preview_timer.stop()
        self.current_points = []
        self.current_widths = []
        self.redo_points = []
        self.polyline_base_index = None
        self.preview_items = []
        self.preview_count = 0
        self.start_marker = None
        self.node_items = []
        self.stroke_items = []
        self.items_by_id = {}
        self.stroke_indices = {}
        self.image_item = None
        self.selected_ids = set()
        self.selected_index = -1
        self.continue_index = -1
        self.drag_start = None
        self.rubber_item = None
        self.rubber_start = None
        self.erasing = False
        self.erased_ids.clear()
        self.eraser_point = None
        self.cursor_point = None
        self.hover_endpoint = None
        self.keyboard_drawing = False
        self.node_after = None
        self.panning = False
        self.space_held = False
        self.graphics_scene.clear()
        self.undo_stack.clear()

    def load_image(self, image, strokes=None):
        self.reset_scene()
        self.image = image.copy()
        self.strokes = copy.deepcopy(strokes or [])
        self.image_item = self.graphics_scene.addPixmap(QPixmap.fromImage(self.image))
        self.graphics_scene.setSceneRect(QRectF(self.image.rect()))
        self.setSceneRect(QRectF(self.image.rect()))
        self.resetTransform()
        self.update_view()
        self.fit_image()
        self.strokes_changed.emit()
        self.selection_changed.emit(-1)

    def show_empty_state(self):
        self.reset_scene()
        self.image = None
        self.strokes = []
        self.resetTransform()
        text = QGraphicsTextItem("No available regions")
        text.setFont(QFont("Arial", 24, QFont.Weight.Bold))
        text.setDefaultTextColor(QColor("gray"))
        bounds = text.boundingRect()
        text.setPos(-bounds.width() / 2, -bounds.height() / 2)
        self.graphics_scene.addItem(text)
        self.graphics_scene.setSceneRect(text.sceneBoundingRect())
        self.setSceneRect(text.sceneBoundingRect())
        self.strokes_changed.emit()
        self.selection_changed.emit(-1)

    def update_view(self):
        if self.image is None:
            return
        self.image_item.setOpacity(0 if self.view_mode == "Traces only" else self.image_opacity)
        self.stroke_indices = {stroke["id"]: index for index, stroke in enumerate(self.strokes)}
        for identifier in self.items_by_id.keys() - self.stroke_indices.keys():
            self.graphics_scene.removeItem(self.items_by_id.pop(identifier))
        self.stroke_items = []
        for stroke in self.strokes:
            item = self.items_by_id.get(stroke["id"])
            if item is None:
                item = StrokeItem(self, stroke)
                self.items_by_id[stroke["id"]] = item
            elif item.stroke is not stroke:
                item.set_stroke(stroke)
            item.setVisible(self.view_mode != "Image only" and stroke["id"] not in self.erased_ids)
            item.highlight(stroke["id"] in self.selected_ids)
            self.stroke_items.append(item)
        self.update_handles()

    def update_handles(self):
        for item in self.node_items:
            self.graphics_scene.removeItem(item)
        self.node_items = []
        if self.view_mode == "Image only" or len(self.selected_ids) != 1 or self.selected_index < 0:
            return
        points = self.strokes[self.selected_index]["points"]
        if self.tool == "edit" and self.show_nodes:
            for index, point in enumerate(points):
                handle = NodeHandle(self, index, point)
                self.graphics_scene.addItem(handle)
                self.node_items.append(handle)
        elif self.tool == "draw" and self.strokes[self.selected_index]["tool"] == "draw":
            for point in (points[0], points[-1]):
                handle = self.make_marker(point, "#00b7ff")
                self.node_items.append(handle)

    def make_marker(self, point, color):
        marker = QGraphicsEllipseItem(-5, -5, 10, 10)
        marker.setPen(QPen(QColor(color), 2))
        marker.setBrush(QBrush(QColor("#1e1e1e")))
        marker.setFlag(marker.GraphicsItemFlag.ItemIgnoresTransformations)
        marker.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        marker.setPos(*point)
        marker.setZValue(4)
        self.graphics_scene.addItem(marker)
        return marker

    def fit_image(self):
        if self.image is not None:
            self.zoom_animation.stop()
            self.ensure_pan_area()
            self.fitInView(QRectF(self.image.rect()), Qt.AspectRatioMode.KeepAspectRatio)
            self.centerOn(QRectF(self.image.rect()).center())
            self.view_changed.emit()

    def ensure_pan_area(self):
        if self.image is None:
            return
        center = self.mapToScene(self.viewport().rect().center())
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        margin = max(visible.width(), visible.height(), self.image.width(), self.image.height()) * 2
        needed = visible.adjusted(-margin, -margin, margin, margin).united(QRectF(self.image.rect()))
        if not self.sceneRect().contains(visible.adjusted(-margin / 2, -margin / 2, margin / 2, margin / 2)):
            self.setSceneRect(self.sceneRect().united(needed))
            self.centerOn(center)

    def zoom(self, factor, position=None):
        if self.image is None or not isinstance(factor, (int, float)) or not math.isfinite(factor) or factor <= 0:
            return
        target = self.zoom_animation.endValue() if self.zoom_animation.state() == self.zoom_animation.State.Running else self.transform().m11()
        self.zoom_animation.stop()
        if self.tool in ("draw", "erase"):
            self.finish_drawing()
        self.finish_drag()
        self.finish_selection()
        self.zoom_position = QPointF(position) if position is not None else QPointF(self.viewport().rect().center())
        self.zoom_anchor = self.scene_position(self.zoom_position)
        self.zoom_animation.setStartValue(self.transform().m11())
        self.zoom_animation.setEndValue(max(0.05, min(50, target * factor)))
        self.zoom_animation.start()

    def animate_zoom(self, value):
        if self.image is None or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            return
        if self.transform().m11() <= 0 or not hasattr(self, "zoom_anchor"):
            return
        factor = value / self.transform().m11()
        self.setTransformationAnchor(self.ViewportAnchor.NoAnchor)
        self.scale(factor, factor)
        self.ensure_pan_area()
        center = self.scene_position(QPointF(self.viewport().rect().center()))
        self.centerOn(center + self.zoom_anchor - self.scene_position(self.zoom_position))
        self.setTransformationAnchor(self.ViewportAnchor.AnchorUnderMouse)
        self.view_changed.emit()

    def animate_stroke(self, color):
        item = self.items_by_id.get(self.animated_stroke)
        if item is not None and isinstance(color, QColor) and color.isValid():
            item.set_color(color)

    def finish_stroke_animation(self):
        self.stroke_animation.stop()
        item = self.items_by_id.get(self.animated_stroke)
        if item is not None:
            item.set_color(QColor("#00b7ff" if item.stroke_id in self.selected_ids else "#ff28b5"))
        self.animated_stroke = None

    def set_view_mode(self, mode):
        self.finish_drawing()
        self.view_mode = mode
        self.update_view()

    def set_tool(self, tool):
        self.finish_drawing()
        self.hover_endpoint = None
        self.tool = tool
        self.finish_pan()
        self.update_handles()
        self.viewport().update()

    def select_strokes(self, identifiers):
        self.finish_stroke_animation()
        self.selected_ids = set(identifiers) & self.stroke_indices.keys()
        self.selected_index = min((self.stroke_indices[key] for key in self.selected_ids), default=-1)
        for item in self.stroke_items:
            item.highlight(item.stroke_id in self.selected_ids)
        self.update_handles()
        self.selection_changed.emit(self.selected_index)

    def select_stroke(self, index):
        self.select_strokes({self.strokes[index]["id"]} if 0 <= index < len(self.strokes) else set())

    def replace_strokes(self, strokes):
        self.finish_stroke_animation()
        self.hover_endpoint = None
        self.cancel_drawing()
        self.strokes = list(strokes)
        self.selected_ids.clear()
        self.selected_index = -1
        self.update_view()
        self.strokes_changed.emit()
        self.selection_changed.emit(-1)

    def apply_changes(self, changes):
        self.finish_stroke_animation()
        self.hover_endpoint = None
        for index, before, after in sorted(changes, reverse=True, key=lambda change: change[0]):
            if before is not None:
                self.strokes.pop(index)
        for index, before, after in sorted(changes, key=lambda change: change[0]):
            if after is not None:
                self.strokes.insert(index, after)
        self.selected_ids = {after["id"] for _, _, after in changes if after is not None}
        self.selected_index = min((index for index, stroke in enumerate(self.strokes)
                                   if stroke["id"] in self.selected_ids), default=-1)
        self.update_view()
        self.strokes_changed.emit()
        self.selection_changed.emit(self.selected_index)

    def add_stroke(self, tool, points, widths=None):
        if not points:
            return
        stroke = {"id": uuid.uuid4().hex, "tool": tool, "points": points}
        set_widths(stroke, widths if widths is not None else [self.current_width] * len(points))
        self.undo_stack.push(StrokeCommand(self, len(self.strokes), None, stroke, "Add stroke"))
        self.animated_stroke = stroke["id"]
        self.stroke_animation.setStartValue(QColor("#ff28b5"))
        self.stroke_animation.setEndValue(QColor("#00b7ff"))
        self.stroke_animation.start()

    def paste_strokes(self, strokes):
        self.finish_drawing()
        changes = []
        for offset, source in enumerate(strokes):
            stroke = copy.deepcopy(source)
            stroke["id"] = uuid.uuid4().hex
            changes.append((len(self.strokes) + offset, None, stroke))
        if changes:
            self.undo_stack.push(StrokeGroupCommand(self, changes, "Paste strokes"))

    def finish_drawing(self):
        if self.node_after is not None and self.selected_index >= 0:
            self.commit_node_move(self.strokes[self.selected_index])
        self.finish_erase()
        self.finish_drag()
        self.finish_selection()
        if not self.current_points:
            self.redo_points = []
            return
        points = rounded_polyline(self.current_points, self.current_radius) if self.tool == "polyline" else self.current_points
        widths = remap_widths(self.current_points, self.current_widths, points)
        index, at_start = self.continue_index, self.continue_start
        self.cancel_drawing()
        if index >= 0:
            if len(points) > 1:
                before = self.strokes[index]
                after = dict(before)
                after["points"] = (list(reversed(points[1:])) + before["points"] if at_start
                                   else before["points"] + points[1:])
                previous_widths = point_widths(before)
                set_widths(after, list(reversed(widths[1:])) + previous_widths if at_start else previous_widths + widths[1:])
                self.undo_stack.push(StrokeCommand(self, index, before, after, "Continue stroke"))
        else:
            self.add_stroke(self.tool, points, widths)

    def cancel_drawing(self):
        self.preview_timer.stop()
        self.current_points = []
        self.current_widths = []
        self.redo_points = []
        self.polyline_base_index = None
        self.continue_index = -1
        self.stroke_filter = None
        for item in self.preview_items:
            self.graphics_scene.removeItem(item)
        self.preview_items = []
        self.preview_count = 0
        if self.start_marker is not None:
            self.graphics_scene.removeItem(self.start_marker)
            self.start_marker = None

    def clear_strokes(self):
        self.finish_drawing()
        if self.strokes:
            self.undo_stack.push(ClearCommand(self))

    def undo(self):
        if self.tool == "polyline" and (self.current_points or self.redo_points):
            if not self.current_points:
                redo, base = self.redo_points, self.polyline_base_index
                continuation = self.continue_index, self.continue_start
                self.undo_stack.undo()
                self.redo_points, self.polyline_base_index = redo, base
                self.continue_index, self.continue_start = continuation
                return
            points = self.current_points[:-1]
            widths = self.current_widths[:-1]
            redo = self.redo_points + [(self.current_points[-1], self.current_widths[-1])]
            continuation = self.continue_index, self.continue_start
            base = self.polyline_base_index
            self.cancel_drawing()
            self.current_points = points
            self.current_widths = widths
            self.continue_index, self.continue_start = continuation
            self.redo_points = redo
            self.polyline_base_index = base
            self.update_preview()
        else:
            self.finish_drawing()
            self.undo_stack.undo()

    def redo(self):
        if self.tool == "polyline" and self.redo_points:
            if self.polyline_base_index is not None and self.undo_stack.index() < self.polyline_base_index:
                redo, base = self.redo_points, self.polyline_base_index
                continuation = self.continue_index, self.continue_start
                self.undo_stack.redo()
                self.redo_points, self.polyline_base_index = redo, base
                self.continue_index, self.continue_start = continuation
                return
            point, width = self.redo_points.pop()
            self.current_points.append(point)
            self.current_widths.append(width)
            self.update_preview()
            return
        if self.tool == "polyline" and self.current_points:
            return
        self.finish_drawing()
        self.undo_stack.redo()

    def delete_selected(self):
        self.finish_drawing()
        changes = [(index, stroke, None) for index, stroke in enumerate(self.strokes)
                   if stroke["id"] in self.selected_ids]
        if changes:
            self.undo_stack.push(StrokeGroupCommand(self, changes, "Delete strokes"))

    def select_all(self):
        self.finish_drawing()
        self.select_strokes(self.stroke_indices.keys())

    def change_selected(self, text, update):
        self.finish_drawing()
        changes = []
        for index, before in enumerate(self.strokes):
            if before["id"] in self.selected_ids:
                after = dict(before)
                update(after)
                if after != before:
                    changes.append((index, before, after))
        if changes:
            self.undo_stack.push(StrokeGroupCommand(self, changes, text))

    def set_selected_width(self, width):
        self.change_selected("Stroke width", lambda stroke: set_widths(stroke, [width] * len(stroke["points"])))

    def smooth_current(self):
        self.change_selected("Smooth stroke", lambda stroke: reshape_stroke(stroke, smooth_stroke))

    def transform_selected(self, scale=1.0, angle=0.0):
        self.finish_drawing()
        if not self.selected_ids:
            return
        rects = [self.items_by_id[identifier].point_bounds for identifier in self.selected_ids]
        bounds = QRectF(QPointF(min(rect.left() for rect in rects), min(rect.top() for rect in rects)),
                        QPointF(max(rect.right() for rect in rects), max(rect.bottom() for rect in rects)))
        center = bounds.center()
        transform = QTransform().translate(center.x(), center.y()).rotate(angle).scale(scale, scale).translate(-center.x(), -center.y())
        transformed = {}
        for identifier in self.selected_ids:
            points = [transform.map(QPointF(*point)) for point in self.strokes[self.stroke_indices[identifier]]["points"]]
            if any(not QRectF(self.image.rect()).contains(point) for point in points):
                self.message.emit("The transformed selection would leave the crop.")
                return
            transformed[identifier] = [[round(point.x(), 3), round(point.y(), 3)] for point in points]
        self.change_selected("Transform strokes", lambda stroke: stroke.update(points=transformed[stroke["id"]]))

    def simplify_selected(self):
        self.change_selected("Simplify strokes", lambda stroke: reshape_stroke(stroke, lambda points: simplify_points(points, self.tolerance)))

    def clamp_point(self, point):
        if self.image is None:
            return QPointF(point)
        return QPointF(max(0, min(point.x(), self.image.width())),
                       max(0, min(point.y(), self.image.height())))

    def scene_position(self, position):
        return self.viewportTransform().inverted()[0].map(QPointF(position))

    def scene_point(self, position):
        if self.image is None or not QRectF(self.viewport().rect()).contains(QPointF(position)):
            return None
        point = self.scene_position(position)
        if not QRectF(self.image.rect()).contains(point):
            return None
        return [round(point.x(), 3), round(point.y(), 3)]

    def move_node(self, index, point):
        before = self.strokes[self.selected_index]
        if self.node_after is None:
            self.node_after = dict(before)
            self.node_after["points"] = list(before["points"])
        self.node_after["points"][index] = [round(point.x(), 3), round(point.y(), 3)]
        if len(before["points"]) > 2 and before["points"][0] == before["points"][-1] and index in (0, len(before["points"]) - 1):
            other = len(before["points"]) - 1 if index == 0 else 0
            self.node_after["points"][other] = list(self.node_after["points"][index])
            self.node_items[other].setPos(point)
        self.stroke_items[self.selected_index].set_stroke(self.node_after)
        self.stroke_items[self.selected_index].highlight(True)

    def commit_node_move(self, before):
        after = self.node_after
        self.node_after = None
        index = self.stroke_indices.get(before["id"], -1)
        if index >= 0 and self.strokes[index] is before and after is not None and after != before:
            self.undo_stack.push(StrokeCommand(self, index, before, after, "Move node"))

    def insert_node(self, position):
        point = self.scene_point(position)
        if point is None or len(self.selected_ids) != 1:
            return
        before = self.strokes[self.selected_index]
        if len(before["points"]) < 2:
            return
        segment = min(range(len(before["points"]) - 1),
                      key=lambda index: segment_distance_squared(point, before["points"][index], before["points"][index + 1]))
        if segment_distance_squared(point, before["points"][segment], before["points"][segment + 1]) > (8 / self.transform().m11()) ** 2:
            return
        after = dict(before)
        after["points"] = list(before["points"])
        after["points"].insert(segment + 1, point)
        if "widths" in before:
            widths = list(before["widths"])
            length = math.sqrt(distance_squared(before["points"][segment], before["points"][segment + 1]))
            fraction = min(1, math.sqrt(distance_squared(before["points"][segment], point)) / length) if length else 0
            widths.insert(segment + 1, round(widths[segment] + (widths[segment + 1] - widths[segment]) * fraction, 3))
            set_widths(after, widths)
        self.undo_stack.push(StrokeCommand(self, self.selected_index, before, after, "Insert node"))

    def update_preview(self):
        if not self.current_points:
            return
        points_to_draw = rounded_polyline(self.current_points, self.current_radius) if self.tool == "polyline" else self.current_points
        widths_to_draw = remap_widths(self.current_points, self.current_widths, points_to_draw) if self.tool == "polyline" else self.current_widths
        if self.tool == "polyline":
            for item in self.preview_items:
                self.graphics_scene.removeItem(item)
            self.preview_items = []
            self.preview_count = 0
        first_chunk = max(0, (self.preview_count - 1) // 128)
        last_chunk = max(0, (len(points_to_draw) - 2) // 128)
        for chunk in range(first_chunk, last_chunk + 1):
            if chunk == len(self.preview_items):
                item = TracePathItem(self)
                item.setZValue(2)
                self.preview_items.append(item)
            item = self.preview_items[chunk]
            points = points_to_draw[chunk * 128:chunk * 128 + 129]
            widths = widths_to_draw[chunk * 128:chunk * 128 + 129]
            variable = any(value != widths[0] for value in widths)
            item.setPath(painter_path(points, widths[0], widths if variable else None))
            color = QColor("#ff28b5")
            item.setPen(QPen(color, widths[0], Qt.PenStyle.SolidLine,
                             Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin) if len(points) > 1 and not variable else QPen(Qt.PenStyle.NoPen))
            item.setBrush(QBrush(color) if len(points) == 1 or variable else QBrush(Qt.BrushStyle.NoBrush))
        self.preview_count = len(points_to_draw)
        if self.tool == "polyline" and self.start_marker is None:
            self.start_marker = self.make_marker(self.current_points[0], "#56c78d")

    def strokes_at(self, point, radius):
        area = QPainterPath()
        area.addEllipse(QPointF(*point), radius, radius)
        return [item for item in self.graphics_scene.items(area) if isinstance(item, StrokeItem)]

    def stroke_index_at(self, position):
        point = self.scene_point(position)
        if point is None or self.view_mode == "Image only":
            return -1
        items = self.strokes_at(point, 6 / self.transform().m11())
        return self.stroke_indices[items[0].stroke_id] if items else -1

    def endpoint_at(self, position):
        point = self.scene_point(position)
        if point is None or self.view_mode == "Image only":
            return None
        radius = 9 / self.transform().m11()
        nearest, limit = None, radius * radius
        for item in self.strokes_at(point, radius):
            stroke = item.stroke
            if stroke["tool"] != self.tool or (len(stroke["points"]) > 2 and stroke["points"][0] == stroke["points"][-1]):
                continue
            for at_start, endpoint in ((False, stroke["points"][-1]), (True, stroke["points"][0])):
                distance = distance_squared(point, endpoint)
                if distance <= limit:
                    nearest, limit = (self.stroke_indices[item.stroke_id], at_start, endpoint), distance
        return nearest

    def start_drawing(self, position, modifiers, timestamp=None):
        self.zoom_animation.stop()
        point = self.scene_point(position)
        if point is None or self.view_mode == "Image only":
            return
        if self.tool == "erase":
            self.finish_erase()
            self.erasing = True
            self.erase_at(point)
        elif self.tool == "polyline":
            if self.current_points and distance_squared(point, self.current_points[-1]) <= (3 / self.transform().m11()) ** 2:
                self.finish_drawing()
                return
            if self.current_points and distance_squared(point, self.current_points[0]) <= (9 / self.transform().m11()) ** 2:
                if len(self.current_points) >= 3:
                    self.current_points.append(list(self.current_points[0]))
                    self.current_widths.append(self.current_widths[0])
                    self.finish_drawing()
                return
            if not self.current_points:
                self.continue_index = -1
                self.current_width = self.brush_width
                self.current_radius = self.corner_radius
                self.polyline_base_index = self.undo_stack.index()
                endpoint = self.endpoint_at(position) if modifiers & Qt.KeyboardModifier.ShiftModifier else None
                if endpoint is not None:
                    self.continue_index, self.continue_start, point = endpoint
                    self.select_stroke(self.continue_index)
                    self.current_width = point_widths(self.strokes[self.continue_index])[0 if self.continue_start else -1]
                self.drawing_width = self.brush_width
            self.redo_points = []
            self.current_widths.append(self.next_width(point) if self.current_points else self.current_width)
            self.current_points.append(point)
            self.update_preview()
        elif self.tool == "draw":
            self.finish_drawing()
            self.current_width = self.brush_width
            endpoint = self.endpoint_at(position) if modifiers & Qt.KeyboardModifier.ShiftModifier else None
            if endpoint is not None:
                self.continue_index, self.continue_start, point = endpoint
                self.select_stroke(self.continue_index)
                self.current_width = point_widths(self.strokes[self.continue_index])[0 if self.continue_start else -1]
            self.drawing_width = self.brush_width
            self.current_points = [point]
            self.current_widths = [self.current_width]
            self.stroke_filter = StrokeFilter(point, timestamp or time.perf_counter()) if self.smoothing else None
            self.update_preview()

    def set_brush_width(self, width):
        self.brush_width = width
        if self.current_points:
            self.drawing_width = width

    def next_width(self, point):
        previous = self.current_widths[-1]
        distance = math.sqrt(distance_squared(self.current_points[-1], point))
        return round(previous + max(-distance, min(distance, self.drawing_width - previous)), 3)

    def sample_drawing(self, position, final=False, timestamp=None):
        point = self.scene_point(position)
        if point is None:
            if self.tool == "draw":
                self.finish_drawing()
            self.eraser_point = None
            return
        if self.erasing:
            self.erase_at(point)
        elif self.tool == "draw" and self.current_points:
            if self.stroke_filter is not None and not final:
                point = self.stroke_filter.sample(point, timestamp or time.perf_counter())
                point = [round(value, 3) for value in point]
            if distance_squared(point, self.current_points[-1]) >= (0.000001 if final else 0.04):
                self.current_widths.append(self.next_width(point))
                self.current_points.append(point)
                if not self.preview_timer.isActive():
                    self.preview_timer.start()

    def erase_at(self, point):
        radius = self.eraser_size / (2 * self.transform().m11())
        path = QPainterPath()
        if self.eraser_point is None:
            path.addEllipse(QPointF(*point), radius, radius)
        else:
            path.moveTo(*self.eraser_point)
            path.lineTo(*point)
            stroker = QPainterPathStroker()
            stroker.setWidth(2 * radius)
            stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
            path = stroker.createStroke(path)
        for item in self.graphics_scene.items(path):
            if isinstance(item, StrokeItem):
                self.erased_ids.add(item.stroke_id)
                item.hide()
        self.eraser_point = point

    def finish_erase(self):
        self.erase_timer.stop()
        if not self.erasing:
            return
        self.erasing = False
        changes = [(index, stroke, None) for index, stroke in enumerate(self.strokes)
                   if stroke["id"] in self.erased_ids]
        self.erased_ids.clear()
        self.eraser_point = None
        if changes:
            self.undo_stack.push(StrokeGroupCommand(self, changes, "Erase strokes"))

    def finish_drag(self):
        if self.drag_start is None:
            return
        self.drag_start = None
        delta = self.drag_delta
        changes = []
        for index, before in enumerate(self.strokes):
            if before["id"] in self.selected_ids:
                self.stroke_items[index].setPos(0, 0)
                if not delta.isNull():
                    after = dict(before)
                    after["points"] = [[round(x + delta.x(), 3), round(y + delta.y(), 3)] for x, y in before["points"]]
                    changes.append((index, before, after))
        if changes:
            self.undo_stack.push(StrokeGroupCommand(self, changes, "Move strokes"))
        else:
            self.update_handles()

    def finish_selection(self):
        if self.rubber_item is not None:
            identifiers = {item.stroke_id for item in self.graphics_scene.items(self.rubber_item.rect())
                           if isinstance(item, StrokeItem)}
            self.graphics_scene.removeItem(self.rubber_item)
            self.rubber_item = None
            self.rubber_start = None
            self.select_strokes(self.rubber_selection | identifiers)

    def start_pan(self, position):
        self.zoom_animation.stop()
        if self.tool in ("draw", "erase"):
            self.finish_drawing()
        self.finish_drag()
        self.finish_selection()
        self.panning = True
        self.pan_start = position
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def move_pan(self, position):
        delta = position - self.pan_start
        self.pan_by(delta.x(), delta.y())
        self.pan_start = position

    def pan_by(self, x, y):
        self.zoom_animation.stop()
        self.ensure_pan_area()
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - round(x))
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() - round(y))

    def finish_pan(self):
        self.panning = False
        self.setCursor(Qt.CursorShape.OpenHandCursor if self.tool == "pan" or self.space_held else
                       Qt.CursorShape.ArrowCursor if self.tool == "edit" else Qt.CursorShape.CrossCursor)

    def mousePressEvent(self, event):
        if self.image is None:
            return
        self.setFocus()
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton) or (
                event.button() == Qt.MouseButton.LeftButton and (self.tool == "pan" or self.space_held)):
            self.start_pan(event.position())
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        if self.tool != "edit":
            self.start_drawing(event.position(), event.modifiers(), event.timestamp() / 1000)
            return
        if self.scene_point(event.position()) is None or self.view_mode == "Image only":
            return
        if isinstance(self.itemAt(event.position().toPoint()), NodeHandle) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            return super().mousePressEvent(event)
        index = self.stroke_index_at(event.position())
        additive = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if index >= 0:
            identifier = self.strokes[index]["id"]
            if additive:
                self.select_strokes(self.selected_ids ^ {identifier})
                return
            if identifier not in self.selected_ids:
                self.select_stroke(index)
            self.drag_start = self.scene_position(event.position())
            self.drag_delta = QPointF()
            bounds = [self.items_by_id[key].point_bounds for key in self.selected_ids]
            self.drag_bounds = QRectF(QPointF(min(rect.left() for rect in bounds), min(rect.top() for rect in bounds)),
                                     QPointF(max(rect.right() for rect in bounds), max(rect.bottom() for rect in bounds)))
            for item in self.node_items:
                item.hide()
        else:
            self.rubber_start = self.scene_position(event.position())
            self.rubber_selection = set(self.selected_ids) if additive else set()
            self.select_strokes(self.rubber_selection)
            self.rubber_item = self.graphics_scene.addRect(QRectF(self.rubber_start, self.rubber_start),
                                                          QPen(QColor("#00b7ff"), 0), QBrush(QColor(0, 183, 255, 30)))
            self.rubber_item.setZValue(5)

    def mouseMoveEvent(self, event):
        if self.keyboard_drawing:
            event = QMouseEvent(event.type(), event.position(), event.globalPosition(),
                                Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, event.modifiers())
        self.update_cursor(event.position())
        if self.panning:
            self.move_pan(event.position())
            return
        if self.tool == "erase":
            self.hover_erase(event.position())
            return
        if self.drag_start is not None:
            delta = self.scene_position(event.position()) - self.drag_start
            delta.setX(max(-self.drag_bounds.left(), min(delta.x(), self.image.width() - self.drag_bounds.right())))
            delta.setY(max(-self.drag_bounds.top(), min(delta.y(), self.image.height() - self.drag_bounds.bottom())))
            self.drag_delta = delta
            for identifier in self.selected_ids:
                self.items_by_id[identifier].setPos(delta)
            return
        if self.rubber_item is not None:
            point = self.clamp_point(self.scene_position(event.position()))
            self.rubber_item.setRect(QRectF(self.rubber_start, point).normalized())
            return
        if event.buttons() & Qt.MouseButton.LeftButton and self.tool in ("draw", "erase"):
            self.sample_drawing(event.position(), timestamp=event.timestamp() / 1000)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.panning:
            self.finish_pan()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if self.rubber_item is not None:
                self.finish_selection()
                return
            if self.drag_start is not None:
                self.finish_drag()
                return
            if self.tool in ("draw", "erase"):
                self.sample_drawing(event.position(), final=True, timestamp=event.timestamp() / 1000)
                self.finish_drawing()
                return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.tool == "polyline" and event.button() == Qt.MouseButton.LeftButton:
            self.finish_drawing()
            return
        if self.tool == "edit" and self.show_nodes and event.button() == Qt.MouseButton.LeftButton:
            self.finish_drag()
            self.insert_node(event.position())
            return
        self.mousePressEvent(event)

    def viewportEvent(self, event):
        if event.type() in (QEvent.Type.TabletPress, QEvent.Type.TabletMove, QEvent.Type.TabletRelease) and self.tool != "edit":
            self.update_cursor(event.position())
            if event.type() == QEvent.Type.TabletPress:
                self.setFocus()
                if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton) or self.tool == "pan" or self.space_held:
                    self.start_pan(event.position())
                elif event.button() == Qt.MouseButton.LeftButton:
                    self.start_drawing(event.position(), event.modifiers(), event.timestamp() / 1000)
            elif event.type() == QEvent.Type.TabletMove:
                if self.panning:
                    self.move_pan(event.position())
                elif self.tool == "erase":
                    self.hover_erase(event.position())
                elif event.buttons() & Qt.MouseButton.LeftButton:
                    self.sample_drawing(event.position(), timestamp=event.timestamp() / 1000)
            elif event.type() == QEvent.Type.TabletRelease:
                if self.panning:
                    self.finish_pan()
                elif event.button() == Qt.MouseButton.LeftButton:
                    self.sample_drawing(event.position(), final=True, timestamp=event.timestamp() / 1000)
                    if self.tool != "polyline":
                        self.finish_drawing()
            event.accept()
            return True
        if event.type() == QEvent.Type.Leave:
            self.update_cursor(None)
            if self.tool in ("draw", "erase"):
                self.finish_drawing()
        return super().viewportEvent(event)

    def focusOutEvent(self, event):
        self.keyboard_click(False)
        if self.tool in ("draw", "erase"):
            self.finish_drawing()
        self.finish_drag()
        self.finish_selection()
        self.space_held = False
        self.finish_pan()
        super().focusOutEvent(event)

    def update_cursor(self, position):
        previous = self.hover_endpoint
        self.hover_endpoint = (self.endpoint_at(position) if position is not None and self.tool in ("draw", "polyline")
                               and not self.current_points else None)
        for endpoint in (previous, self.hover_endpoint):
            if endpoint is not None:
                point = self.viewportTransform().map(QPointF(*endpoint[2]))
                self.viewport().update(QRectF(point.x() - 12, point.y() - 12, 24, 24).toAlignedRect())
        if self.tool == "erase":
            radius = self.eraser_size / 2 + 2
            for point in (self.cursor_point, position):
                if point is not None:
                    self.viewport().update(QRectF(point.x() - radius, point.y() - radius, radius * 2, radius * 2).toAlignedRect())
        self.cursor_point = position

    def hover_erase(self, position):
        if self.space_held or not self.isEnabled() or self.view_mode == "Image only":
            return
        point = self.scene_point(position)
        if point is None:
            self.finish_erase()
            return
        self.erasing = True
        self.erase_at(point)
        self.erase_timer.start()

    def drawForeground(self, painter, rect):
        super().drawForeground(painter, rect)
        if self.hover_endpoint is not None and self.tool in ("draw", "polyline") and not self.current_points:
            painter.save()
            painter.resetTransform()
            painter.setPen(QPen(QColor("#56c78d"), 2))
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            painter.drawEllipse(self.viewportTransform().map(QPointF(*self.hover_endpoint[2])), 7, 7)
            painter.restore()
        if self.tool == "erase" and self.cursor_point is not None and self.scene_point(self.cursor_point) is not None:
            painter.save()
            painter.resetTransform()
            painter.setPen(QPen(QColor("#ffffff"), 1))
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            painter.drawEllipse(self.cursor_point, self.eraser_size / 2, self.eraser_size / 2)
            painter.restore()

    def wheelEvent(self, event):
        delta = event.angleDelta().y() / 120 if event.angleDelta().y() else event.pixelDelta().y() / 40
        if delta:
            self.zoom(1.15 ** delta, event.position())

    def keyboard_click(self, pressed, modifiers=Qt.KeyboardModifier.NoModifier):
        if pressed == self.keyboard_drawing:
            return
        position = self.viewport().mapFromGlobal(QCursor.pos())
        if pressed and (not self.isEnabled() or not self.viewport().rect().contains(position)):
            return
        self.keyboard_drawing = pressed
        event = QMouseEvent(QEvent.Type.MouseButtonPress if pressed else QEvent.Type.MouseButtonRelease,
                            QPointF(position), QPointF(QCursor.pos()), Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton if pressed else Qt.MouseButton.NoButton, modifiers)
        QApplication.sendEvent(self.viewport(), event)

    def keyPressEvent(self, event):
        if self.pan_binding.count() and event.keyCombination() == self.pan_binding[0]:
            if not event.isAutoRepeat():
                self.space_held = True
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if self.pan_binding.count() and event.key() == self.pan_binding[0].key():
            if not event.isAutoRepeat():
                self.space_held = False
                self.finish_pan()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.image is not None:
            self.ensure_pan_area()
