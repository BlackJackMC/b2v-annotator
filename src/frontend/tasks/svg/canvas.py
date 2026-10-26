import copy
import uuid
from PyQt6.QtCore import Qt, QPoint, QPointF, QRectF, pyqtSignal
from PyQt6.QtGui import QMouseEvent, QWheelEvent, QColor, QBrush, QPainter, QPainterPath, QPainterPathStroker, QPen, QPixmap, QUndoCommand, QUndoStack
from PyQt6.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsPathItem, QGraphicsEllipseItem, QGraphicsSceneMouseEvent
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
        stroker.setWidth(max(1, 8 / self.canvas.transform().m11()))
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