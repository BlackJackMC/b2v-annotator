from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QPolygonF, QPen, QColor, QBrush
from PyQt6.QtWidgets import QGraphicsView, QGraphicsItem
from tasks.aabb.canvas import ImageCanvas as BaseCanvas
from tasks.polygon.strategy import valid_polygon


class ImageCanvas(BaseCanvas):
    def __init__(self):
        super().__init__()
        self.polygons = []
        self.pending = []
        self.selected = None
        self.drag = None
        self.overlays = []
        self.setToolTip("Click four corners in perimeter order. Drag a corner or the polygon interior. Esc cancels unfinished drawing. Delete removes selection. Ctrl+S saves.")

    def reset_interaction(self):
        super().reset_interaction()
        self.pending = []
        self.drag = None
        self.selected = None

    def load_image(self, image_path, crop_coords=None):
        self.overlays = []
        self.polygons = []
        super().load_image(image_path)

    def show_empty_state(self):
        self.overlays = []
        self.polygons = []
        super().show_empty_state()

    def load_existing_points(self, points_data):
        bounds = self.image_item.boundingRect() if self.image_item else None
        if (bounds is None or not isinstance(points_data, list) or
                any(not valid_polygon(points) or any(not bounds.contains(QPointF(p["x"], p["y"])) for p in points)
                    for points in points_data)):
            raise ValueError("Invalid saved polygon or corners outside the image.")
        self.reset_interaction()
        self.polygons = [[dict(p) for p in points] for points in points_data]
        self.redraw()

    def wheelEvent(self, event):
        if not self.is_editable or self.drag or self._is_panning:
            return
        scale = self.transform().m11()
        target = max(0.05, min(32.0, scale * (1.15 if event.angleDelta().y() > 0 else 1 / 1.15)))
        if scale > 0:
            self.scale(target / scale, target / scale)

    def get_coordinates(self):
        return [[dict(p) for p in points] for points in self.polygons]

    def valid_coordinates(self, polygons):
        if self.image_item is None or self.pending or self.drag:
            return False
        bounds = self.image_item.boundingRect()
        return all(valid_polygon(points) and all(bounds.contains(QPointF(p["x"], p["y"])) for p in points) for points in polygons)

    def redraw(self):
        for item in self.overlays:
            self.scene.removeItem(item)
        self.overlays = []
        for index, points in enumerate(self.polygons + ([self.pending] if self.pending else [])):
            color = QColor("cyan" if index == self.selected else "yellow")
            pen = QPen(color, 2)
            pen.setCosmetic(True)
            polygon = QPolygonF([QPointF(p["x"], p["y"]) for p in points])
            item = self.scene.addPolygon(polygon, pen, QBrush(QColor(255, 255, 0, 25)))
            self.overlays.append(item)
            if index == self.selected or index == len(self.polygons):
                for point in polygon:
                    handle = self.scene.addEllipse(-4, -4, 8, 8, pen, QBrush(color))
                    handle.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
                    handle.setPos(point)
                    self.overlays.append(handle)

    def mousePressEvent(self, event):
        if not self.is_editable:
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            if self.drag:
                return
            return super().mousePressEvent(event)
        if self._is_panning:
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        point = self.mapToScene(event.pos())
        if not self.image_item.boundingRect().contains(point):
            return
        if not self.pending:
            order = list(reversed(range(len(self.polygons))))
            if self.selected is not None:
                order.remove(self.selected)
                order.insert(0, self.selected)
            for index in order:
                points = self.polygons[index]
                vertex = next((i for i, p in enumerate(points) if
                    (self.mapFromScene(QPointF(p["x"], p["y"])) - event.pos()).manhattanLength() <= 10), None)
                if vertex is not None or QPolygonF([QPointF(p["x"], p["y"]) for p in points]).containsPoint(point, Qt.FillRule.OddEvenFill):
                    self.selected = index
                    self.drag = (point, [dict(p) for p in points], vertex)
                    self.redraw()
                    return
        if any((self.mapFromScene(QPointF(p["x"], p["y"])) - event.pos()).manhattanLength() <= 3 for p in self.pending):
            return
        candidate = self.pending + [{"x": point.x(), "y": point.y()}]
        if len(candidate) == 4:
            if not valid_polygon(candidate):
                self.message.emit("Use four distinct corners in perimeter order; edges cannot cross.")
                return
            self.polygons.append(candidate)
            self.pending = []
            self.selected = len(self.polygons) - 1
        else:
            self.pending = candidate
            self.selected = None
        self.redraw()

    def mouseMoveEvent(self, event):
        if self._is_panning:
            return super().mouseMoveEvent(event)
        if not self.drag:
            return
        point = self.mapToScene(event.pos())
        bounds = self.image_item.boundingRect()
        origin, points, vertex = self.drag
        candidate = [dict(p) for p in points]
        if vertex is not None:
            candidate[vertex] = {"x": max(bounds.left(), min(point.x(), bounds.right())),
                                 "y": max(bounds.top(), min(point.y(), bounds.bottom()))}
        else:
            dx = max(-min(p["x"] for p in points), min(point.x() - origin.x(), bounds.right() - max(p["x"] for p in points)))
            dy = max(-min(p["y"] for p in points), min(point.y() - origin.y(), bounds.bottom() - max(p["y"] for p in points)))
            candidate = [{"x": p["x"] + dx, "y": p["y"] + dy} for p in points]
        if valid_polygon(candidate):
            self.polygons[self.selected] = candidate
            self.redraw()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag = None
        if self._is_panning and event.button() == Qt.MouseButton.MiddleButton:
            return super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.pending = []
            if self.drag:
                self.polygons[self.selected] = self.drag[1]
            self.drag = None
            self.redraw()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if not self.pending and self.selected is not None and not self.drag:
                self.polygons.pop(self.selected)
                self.selected = None
                self.redraw()
            event.accept()
            return
        QGraphicsView.keyPressEvent(self, event)
