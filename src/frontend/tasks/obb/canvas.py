from typing import List, Dict
from PyQt6.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QGraphicsTextItem
from PyQt6.QtCore import Qt, QPoint, QPointF, pyqtSignal
from PyQt6.QtGui import QPen, QBrush, QColor, QPixmap, QFont, QPainter, QKeySequence

class DraggablePoint(QGraphicsEllipseItem):
    def __init__(self, x, y, canvas, is_first=False):
        super().__init__(-5, -5, 10, 10)
        self.setPos(x, y)
        self.setBrush(QBrush(QColor("red" if is_first else "blue")))
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIsMovable)
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIsSelectable)
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.canvas = canvas
        self.is_first = is_first

    def itemChange(self, change, value):
        if change == QGraphicsEllipseItem.GraphicsItemChange.ItemPositionChange:
            if self.canvas.image_item:
                rect = self.canvas.image_item.boundingRect()
                x = max(0.0, min(value.x(), rect.width()))
                y = max(0.0, min(value.y(), rect.height()))
                return QPointF(x, y)
                
        if change == QGraphicsEllipseItem.GraphicsItemChange.ItemPositionHasChanged:
            self.canvas.update_lines()
            
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        if self.is_first and len(self.canvas.points) == 4 and not self.canvas.is_closed:
            self.canvas.close_polygon()
        super().mousePressEvent(event)


class ImageCanvas(QGraphicsView):
    message = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.points = []
        self.lines = []
        self.is_closed = False
        self.image_item = None
        self.is_editable = False
        self.copied_points = []
        self.copied_image_size = None
        self.setToolTip("Ctrl+A: select all four corners. Ctrl+C: copy OBB. Ctrl+V: replace the current OBB at the original coordinates.")
        
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        
        self._is_panning = False
        self._pan_start = QPoint()

    def wheelEvent(self, event):
        if not self.is_editable:
            return
        
        zoom_in_factor = 1.15
        zoom_out_factor = 1 / zoom_in_factor

        if event.angleDelta().y() > 0:
            zoom_factor = zoom_in_factor
        else:
            zoom_factor = zoom_out_factor

        self.scale(zoom_factor, zoom_factor)

    def mouseMoveEvent(self, event):
        if self._is_panning:
            delta = event.pos() - self._pan_start
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            self._pan_start = event.pos()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._is_panning:
            self._is_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        super().mouseReleaseEvent(event)

    def mousePressEvent(self, event):
        if not self.is_editable:
            return

        if event.button() == Qt.MouseButton.RightButton or event.button() == Qt.MouseButton.MiddleButton:
            self._is_panning = True
            self._pan_start = event.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        if self.is_closed:
            super().mousePressEvent(event)
            return

        item = self.itemAt(event.pos())
        if item == self.image_item or item is None:
            scene_pos = self.mapToScene(event.pos())
            x, y = scene_pos.x(), scene_pos.y()
            
            if self.image_item:
                rect = self.image_item.boundingRect()
                x = max(0.0, min(x, rect.width()))
                y = max(0.0, min(y, rect.height()))

            is_first = len(self.points) == 0
            
            if len(self.points) < 4:
                pt = DraggablePoint(x, y, self, is_first)
                self.scene.addItem(pt)
                self.points.append(pt)
                self.update_lines()
        
        super().mousePressEvent(event)

    def show_empty_state(self):
        self.scene.clear()
        self.image_item = None
        self.points.clear()
        self.lines.clear()
        self.is_closed = False
        self.is_editable = False
        self.resetTransform()
        
        text_item = QGraphicsTextItem("No more images available")
        text_item.setFont(QFont("Arial", 24, QFont.Weight.Bold))
        text_item.setDefaultTextColor(QColor("gray"))
        
        text_rect = text_item.boundingRect()
        text_item.setPos(-text_rect.width() / 2, -text_rect.height() / 2)
        self.scene.addItem(text_item)
        self.setSceneRect(text_item.boundingRect())

    def load_image(self, image_path):
        self.scene.clear()
        self.points.clear()
        self.lines.clear()
        self.is_closed = False
        self.is_editable = True
        self.resetTransform()
        
        pixmap = QPixmap(image_path)
        self.image_item = self.scene.addPixmap(pixmap)
        
        margin = max(pixmap.width(), pixmap.height())
        self.setSceneRect(-margin, -margin, pixmap.width() + 2 * margin, pixmap.height() + 2 * margin)
        
        self.centerOn(self.image_item)

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

    def keyPressEvent(self, event):
        if not self.is_editable:
            return super().keyPressEvent(event)
        if event.matches(QKeySequence.StandardKey.SelectAll):
            for point in self.points:
                point.setSelected(True)
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Copy):
            if self.is_closed and len(self.points) == 4 and all(point.isSelected() for point in self.points):
                self.copied_points = self.get_coordinates()
                self.copied_image_size = self.image_item.pixmap().size()
                self.message.emit("Copied OBB with all four corners.")
            else:
                self.message.emit("Select all four corners of a completed OBB with Ctrl+A before copying.")
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            if self.copied_points:
                if self.image_item.pixmap().size() != self.copied_image_size:
                    self.message.emit("Cannot paste: image dimensions differ. Coordinates were not changed.")
                else:
                    self.load_existing_points(self.copied_points)
                    for point in self.points:
                        point.setSelected(True)
                    self.message.emit("Pasted OBB at its original coordinates, replacing the previous OBB.")
            event.accept()
            return
        super().keyPressEvent(event)

    def get_coordinates(self) -> List[Dict]:
        if not self.is_closed: return []
        return [{"x": p.x(), "y": p.y()} for p in self.points]
