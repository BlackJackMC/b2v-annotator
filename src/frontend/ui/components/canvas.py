from typing import List, Dict
from PyQt6.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QGraphicsTextItem
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPen, QBrush, QColor, QPixmap, QFont

class DraggablePoint(QGraphicsEllipseItem):
    def __init__(self, x, y, canvas, is_first=False):
        super().__init__(-5, -5, 10, 10)
        self.setPos(x, y)
        self.setBrush(QBrush(QColor("red" if is_first else "blue")))
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIsMovable)
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.canvas = canvas
        self.is_first = is_first

    def itemChange(self, change, value):
        if change == QGraphicsEllipseItem.GraphicsItemChange.ItemPositionHasChanged:
            self.canvas.update_lines()
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        if self.is_first and len(self.canvas.points) == 4 and not self.canvas.is_closed:
            self.canvas.close_polygon()
        super().mousePressEvent(event)

class ImageCanvas(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.points = []
        self.lines = []
        self.is_closed = False
        self.image_item = None
        self.is_editable = False

    def show_empty_state(self):
        self.scene.clear()
        self.points.clear()
        self.lines.clear()
        self.is_closed = False
        self.is_editable = False
        
        text_item = QGraphicsTextItem("No more images available")
        text_item.setFont(QFont("Arial", 24, QFont.Weight.Bold))
        text_item.setDefaultTextColor(QColor("gray"))
        self.scene.addItem(text_item)

    def load_image(self, image_path):
        self.scene.clear()
        self.points.clear()
        self.lines.clear()
        self.is_closed = False
        self.is_editable = True
        
        pixmap = QPixmap(image_path)
        self.image_item = self.scene.addPixmap(pixmap)
        self.setSceneRect(0, 0, pixmap.width(), pixmap.height())

    def load_existing_points(self, points_data: List[Dict]):
        for pt in self.points:
            self.scene.removeItem(pt)
        self.points.clear()
        
        for i, p_data in enumerate(points_data):
            is_first = (i == 0)
            pt = DraggablePoint(p_data["x"], p_data["y"], self, is_first)
            self.scene.addItem(pt)
            self.points.append(pt)
        
        self.is_closed = True
        self.update_lines()

    def mousePressEvent(self, event):
        if not self.is_editable:
            return

        if self.is_closed:
            super().mousePressEvent(event)
            return

        item = self.itemAt(event.pos())
        if item == self.image_item or item is None:
            scene_pos = self.mapToScene(event.pos())
            is_first = len(self.points) == 0
            
            if len(self.points) < 4:
                pt = DraggablePoint(scene_pos.x(), scene_pos.y(), self, is_first)
                self.scene.addItem(pt)
                self.points.append(pt)
                self.update_lines()
        
        super().mousePressEvent(event)

    def update_lines(self):
        for line in self.lines:
            self.scene.removeItem(line)
        self.lines.clear()

        pen = QPen(QColor("green"), 2)
        for i in range(len(self.points) - 1):
            line = self.scene.addLine(
                self.points[i].x(), self.points[i].y(),
                self.points[i+1].x(), self.points[i+1].y(), pen
            )
            self.lines.append(line)

        if self.is_closed and len(self.points) == 4:
            line = self.scene.addLine(
                self.points[-1].x(), self.points[-1].y(),
                self.points[0].x(), self.points[0].y(), pen
            )
            self.lines.append(line)

    def close_polygon(self):
        self.is_closed = True
        self.update_lines()

    def get_coordinates(self) -> List[Dict]:
        if not self.is_closed: return []
        return [{"x": p.x(), "y": p.y()} for p in self.points]