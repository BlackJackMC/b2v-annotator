import copy
import uuid
from PyQt6.QtCore import Qt, QPoint, QPointF, QRectF, pyqtSignal
from PyQt6.QtGui import QColor, QBrush, QFont, QMouseEvent, QPainter, QPainterPath, QPainterPathStroker, QPen, QPixmap, QUndoCommand, QUndoStack, QWheelEvent
from PyQt6.QtWidgets import QGraphicsEllipseItem, QGraphicsPathItem, QGraphicsScene, QGraphicsSceneMouseEvent, QGraphicsTextItem, QGraphicsView
from tasks.svg.geometry import distance_squared, segment_distance_squared, simplify_points


def painter_path(points):
    path = QPainterPath()
    if len(points) == 1:
        path.addEllipse(QPointF(*points[0]), 0.5, 0.5)
    elif points:
        path.moveTo(*points[0])
        for point in points[1:]:
            path.lineTo(*point)
    return path


class StrokeCommand(QUndoCommand):
    def __init__(self, canvas, index, before, after, text):
        super().__init__(text)
        self.canvas = canvas
        self.index = index
        self.before = copy.deepcopy(before)
        self.after = copy.deepcopy(after)

    def redo(self):
        self.canvas.apply_stroke(self.index, self.before, self.after)

    def undo(self):
        self.canvas.apply_stroke(self.index, self.after, self.before)


class ClearCommand(QUndoCommand):
    def __init__(self, canvas):
        super().__init__("Clear tracing")
        self.canvas = canvas
        self.strokes = copy.deepcopy(canvas.strokes)

    def redo(self):
        self.canvas.replace_strokes([])

    def undo(self):
        self.canvas.replace_strokes(self.strokes)


class StrokeItem(QGraphicsPathItem):
    def __init__(self, canvas, stroke):
        super().__init__(painter_path(stroke["points"]))
        self.canvas = canvas
        self.stroke_id = stroke["id"]
        self.setZValue(1)

    def shape(self):
        stroker = QPainterPathStroker()
        stroker.setWidth(max(1, 8 / max(self.canvas.transform().m11(), 0.01)))
        return stroker.createStroke(self.path())

    def boundingRect(self):
        return super().boundingRect().adjusted(-10, -10, 10, 10)


class NodeHandle(QGraphicsEllipseItem):
    def __init__(self, canvas, index, point):
        super().__init__(-4, -4, 8, 8)
        self.canvas = canvas
        self.index = index
        self.setPos(*point)
        self.setBrush(QBrush(QColor("white")))
        self.setPen(QPen(QColor("#0078d4")))
        self.setZValue(3)
        self.setFlags(self.GraphicsItemFlag.ItemIsMovable |
                      self.GraphicsItemFlag.ItemSendsGeometryChanges |
                      self.GraphicsItemFlag.ItemIgnoresTransformations)
        self.before = None

    def itemChange(self, change, value):
        if change == self.GraphicsItemChange.ItemPositionChange:
            return self.canvas.clamp_point(value)
        if change == self.GraphicsItemChange.ItemPositionHasChanged and self.before is not None:
            self.canvas.move_node(self.index, value)
        return super().itemChange(change, value)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent | None):
        if event is None:
            return
        self.before = copy.deepcopy(self.canvas.strokes[self.canvas.selected_index])
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent | None):
        if event is None:
            return
        super().mouseReleaseEvent(event)
        before = self.before
        self.before = None
        if before is not None:
            self.canvas.commit_node_move(before)


class ImageCanvas(QGraphicsView):
    strokes_changed = pyqtSignal()
    selection_changed = pyqtSignal(int)
    message = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.graphics_scene = QGraphicsScene(self)
        self.setScene(self.graphics_scene)
        self.image = None
        self.image_item = None
        self.stroke_items = []
        self.node_items = []
        self.strokes = []
        self.selected_index = -1
        self.tool = "draw"
        self.view_mode = "Overlay"
        self.image_opacity = 0.65
        self.tolerance = 0.35
        self.current_points = []
        self.preview_item = None
        self.panning = False
        self.pan_start = QPoint()
        self.undo_stack = QUndoStack(self)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(self.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(self.ViewportAnchor.AnchorUnderMouse)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def load_image(self, image, strokes=None):
        self.image = image.copy()
        self.strokes = copy.deepcopy(strokes or [])
        self.selected_index = -1
        self.current_points = []
        self.undo_stack.clear()
        self.resetTransform()
        self.update_view()
        self.fit_image()
        self.strokes_changed.emit()
        self.selection_changed.emit(-1)

    def show_empty_state(self):
        self.image = None
        self.strokes = []
        self.selected_index = -1
        self.current_points = []
        self.undo_stack.clear()
        self.graphics_scene.clear()
        text = QGraphicsTextItem("No available regions")
        text.setFont(QFont("Arial", 24, QFont.Weight.Bold))
        text.setDefaultTextColor(QColor("gray"))
        bounds = text.boundingRect()
        text.setPos(-bounds.width() / 2, -bounds.height() / 2)
        self.graphics_scene.addItem(text)
        self.graphics_scene.setSceneRect(text.sceneBoundingRect())

    def update_view(self):
        if self.image is None:
            return
        self.graphics_scene.clear()
        self.stroke_items = []
        self.node_items = []
        self.preview_item = None
        self.image_item = self.graphics_scene.addPixmap(QPixmap.fromImage(self.image))
        self.image_item.setOpacity(0 if self.view_mode == "Traces only" else self.image_opacity)
        self.image_item.setZValue(0)
        show_traces = self.view_mode != "Image only"
        for index, stroke in enumerate(self.strokes):
            item = StrokeItem(self, stroke)
            color = QColor("#ff28b5" if index != self.selected_index else "#00b7ff")
            item.setPen(QPen(color, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            item.setVisible(show_traces)
            self.graphics_scene.addItem(item)
            self.stroke_items.append(item)
        if show_traces and self.tool == "edit" and 0 <= self.selected_index < len(self.strokes):
            for index, point in enumerate(self.strokes[self.selected_index]["points"]):
                handle = NodeHandle(self, index, point)
                self.graphics_scene.addItem(handle)
                self.node_items.append(handle)
        self.graphics_scene.setSceneRect(QRectF(0, 0, self.image.width(), self.image.height()))
        self.update_preview()

    def fit_image(self):
        if self.image is not None:
            self.fitInView(QRectF(0, 0, self.image.width(), self.image.height()), Qt.AspectRatioMode.KeepAspectRatio)

    def set_view_mode(self, mode):
        self.view_mode = mode
        self.update_view()

    def set_tool(self, tool):
        if tool != self.tool:
            self.finish_drawing()
        self.tool = tool
        self.update_view()

    def select_stroke(self, index):
        index = index if 0 <= index < len(self.strokes) else -1
        if self.selected_index != index:
            self.selected_index = index
            self.selection_changed.emit(index)
            self.update_view()

    def replace_strokes(self, strokes):
        self.strokes = copy.deepcopy(strokes)
        self.selected_index = min(self.selected_index, len(self.strokes) - 1)
        self.current_points = []
        self.update_view()
        self.strokes_changed.emit()
        self.selection_changed.emit(self.selected_index)

    def apply_stroke(self, index, before, after):
        if before is None and after is not None:
            self.strokes.insert(index, copy.deepcopy(after))
        elif after is None and before is not None:
            if 0 <= index < len(self.strokes):
                self.strokes.pop(index)
        elif after is not None and 0 <= index < len(self.strokes):
            self.strokes[index] = copy.deepcopy(after)
        self.selected_index = index if after is not None else min(index, len(self.strokes) - 1)
        self.update_view()
        self.strokes_changed.emit()
        self.selection_changed.emit(self.selected_index)

    def add_stroke(self, tool, points):
        points = simplify_points(points, self.tolerance) if tool == "draw" else [list(point) for point in points]
        if not points:
            return
        stroke = {"id": uuid.uuid4().hex, "tool": tool, "points": points}
        self.undo_stack.push(StrokeCommand(self, len(self.strokes), None, stroke, "Add stroke"))

    def finish_drawing(self):
        if self.current_points:
            points = self.current_points
            self.current_points = []
            self.preview_item = None
            self.add_stroke(self.tool, points)

    def cancel_drawing(self):
        self.current_points = []
        if self.preview_item is not None and self.preview_item.scene() is self.graphics_scene:
            self.graphics_scene.removeItem(self.preview_item)
        self.preview_item = None

    def clear_strokes(self):
        self.cancel_drawing()
        if self.strokes:
            self.undo_stack.push(ClearCommand(self))

    def undo(self):
        if self.current_points:
            self.current_points.pop()
            self.update_preview()
        else:
            self.undo_stack.undo()

    def delete_selected(self):
        if 0 <= self.selected_index < len(self.strokes):
            index = self.selected_index
            self.undo_stack.push(StrokeCommand(self, index, self.strokes[index], None, "Delete stroke"))

    def simplify_selected(self):
        if 0 <= self.selected_index < len(self.strokes):
            before = self.strokes[self.selected_index]
            after = copy.deepcopy(before)
            after["points"] = simplify_points(after["points"], self.tolerance)
            if after != before:
                self.undo_stack.push(StrokeCommand(self, self.selected_index, before, after, "Simplify stroke"))

    def clamp_point(self, point):
        if self.image is None:
            return QPointF(point)
        return QPointF(max(0, min(point.x(), self.image.width())),
                       max(0, min(point.y(), self.image.height())))

    def scene_point(self, position):
        point = self.clamp_point(self.mapToScene(position))
        return [round(point.x(), 3), round(point.y(), 3)]

    def move_node(self, index, point):
        if 0 <= self.selected_index < len(self.strokes) and 0 <= index < len(self.strokes[self.selected_index]["points"]):
            self.strokes[self.selected_index]["points"][index] = [round(point.x(), 3), round(point.y(), 3)]
            self.stroke_items[self.selected_index].setPath(painter_path(self.strokes[self.selected_index]["points"]))

    def commit_node_move(self, before):
        if not 0 <= self.selected_index < len(self.strokes):
            return
        index = self.selected_index
        after = copy.deepcopy(self.strokes[index])
        if after == before:
            return
        self.strokes[index] = copy.deepcopy(before)
        self.undo_stack.push(StrokeCommand(self, index, before, after, "Move node"))

    def insert_node(self, position):
        if not 0 <= self.selected_index < len(self.strokes):
            return
        before = self.strokes[self.selected_index]
        if len(before["points"]) < 2:
            return
        point = self.scene_point(position)
        segment = min(range(len(before["points"]) - 1),
                      key=lambda index: segment_distance_squared(point, before["points"][index], before["points"][index + 1]))
        after = copy.deepcopy(before)
        after["points"].insert(segment + 1, point)
        self.undo_stack.push(StrokeCommand(self, self.selected_index, before, after, "Insert node"))

    def update_preview(self):
        if self.preview_item is not None and self.preview_item.scene() is self.graphics_scene:
            self.graphics_scene.removeItem(self.preview_item)
        self.preview_item = None
        if self.current_points and self.view_mode != "Image only":
            self.preview_item = self.graphics_scene.addPath(
                painter_path(self.current_points),
                QPen(QColor("#ff28b5"), 2, Qt.PenStyle.DashLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            )
            self.preview_item.setZValue(2)

    def stroke_index_at(self, position):
        item = self.itemAt(position)
        while item is not None:
            if isinstance(item, StrokeItem):
                return next((index for index, stroke in enumerate(self.strokes) if stroke["id"] == item.stroke_id), -1)
            item = item.parentItem()
        return -1

    def mousePressEvent(self, event: QMouseEvent | None):
        if event is None or self.image is None:
            return
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self.panning = True
            self.pan_start = event.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        if self.tool == "edit":
            if isinstance(self.itemAt(event.pos()), NodeHandle):
                return super().mousePressEvent(event)
            self.select_stroke(self.stroke_index_at(event.pos()))
            return super().mousePressEvent(event)
        point = self.scene_point(event.pos())
        if self.tool == "draw":
            self.current_points = [point]
        else:
            self.current_points.append(point)
        self.update_preview()

    def mouseMoveEvent(self, event: QMouseEvent | None):
        if event is None:
            return
        if self.panning:
            delta = event.pos() - self.pan_start
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            self.pan_start = event.pos()
            return
        if self.tool == "draw" and self.current_points and event.buttons() & Qt.MouseButton.LeftButton:
            point = self.scene_point(event.pos())
            if distance_squared(point, self.current_points[-1]) >= 0.25:
                self.current_points.append(point)
                self.update_preview()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent | None):
        if event is None:
            return
        if self.panning:
            self.panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        if self.tool == "draw" and event.button() == Qt.MouseButton.LeftButton and self.current_points:
            self.finish_drawing()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent | None):
        if event is None:
            return
        if self.tool == "edit" and event.button() == Qt.MouseButton.LeftButton:
            self.insert_node(event.pos())
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QWheelEvent | None):
        if event is None or self.image is None:
            return
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        current = self.transform().m11()
        if 0.05 <= current * factor <= 50:
            self.scale(factor, factor)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.image is not None and self.transform().m11() == 1:
            self.fit_image()
