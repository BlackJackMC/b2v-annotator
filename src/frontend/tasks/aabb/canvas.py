from typing import List, Dict
from PyQt6.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsRectItem, QGraphicsTextItem
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui import QPen, QBrush, QColor, QPixmap, QFont, QPainter

class BoundingBox(QGraphicsRectItem):
    def __init__(self, x, y, w, h, canvas):
        super().__init__(x, y, w, h)
        self.canvas = canvas
        self.setPen(QPen(QColor("yellow"), 2))
        self.setBrush(QBrush(QColor(255, 255, 0, 40)))
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsSelectable)
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsMovable)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.canvas.boxes.remove(self)
            self.canvas.scene.removeItem(self)
            return
        super().mousePressEvent(event)

class ImageCanvas(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.boxes = []
        self.image_item = None
        self.is_editable = False
        
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        
        self._is_panning = False
        self._pan_start = QPoint()
        
        self._is_drawing = False
        self._current_box = None
        self._draw_start_pos = None

    def wheelEvent(self, event):
        if not self.is_editable:
            return
        zoom_in_factor = 1.15
        zoom_out_factor = 1 / zoom_in_factor
        zoom_factor = zoom_in_factor if event.angleDelta().y() > 0 else zoom_out_factor
        self.scale(zoom_factor, zoom_factor)

    def mousePressEvent(self, event):
        if not self.is_editable:
            return

        if event.button() == Qt.MouseButton.MiddleButton:
            self._is_panning = True
            self._pan_start = event.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        item = self.itemAt(event.pos())
        if event.button() == Qt.MouseButton.LeftButton:
            if item == self.image_item or item is None:
                self._is_drawing = True
                self._draw_start_pos = self.mapToScene(event.pos())
                
                if self.image_item:
                    rect = self.image_item.boundingRect()
                    x = max(0.0, min(self._draw_start_pos.x(), rect.width()))
                    y = max(0.0, min(self._draw_start_pos.y(), rect.height()))
                    self._draw_start_pos.setX(x)
                    self._draw_start_pos.setY(y)

                self._current_box = BoundingBox(self._draw_start_pos.x(), self._draw_start_pos.y(), 0, 0, self)
                self.scene.addItem(self._current_box)
                self.boxes.append(self._current_box)
            else:
                super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._is_panning:
            delta = event.pos() - self._pan_start
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            self._pan_start = event.pos()
            return
            
        if self._is_drawing and self._current_box:
            current_pos = self.mapToScene(event.pos())
            
            if self.image_item:
                rect = self.image_item.boundingRect()
                x_curr = max(0.0, min(current_pos.x(), rect.width()))
                y_curr = max(0.0, min(current_pos.y(), rect.height()))
            else:
                x_curr, y_curr = current_pos.x(), current_pos.y()

            x = min(self._draw_start_pos.x(), x_curr)
            y = min(self._draw_start_pos.y(), y_curr)
            w = abs(self._draw_start_pos.x() - x_curr)
            h = abs(self._draw_start_pos.y() - y_curr)
            
            self._current_box.setRect(x, y, w, h)
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._is_panning:
            self._is_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
            
        if self._is_drawing:
            self._is_drawing = False
            if self._current_box:
                rect = self._current_box.rect()
                if rect.width() < 5 or rect.height() < 5:
                    self.scene.removeItem(self._current_box)
                    self.boxes.remove(self._current_box)
            self._current_box = None
            return

        super().mouseReleaseEvent(event)

    def show_empty_state(self):
        self.scene.clear()
        self.boxes.clear()
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
        self.boxes.clear()
        self.is_editable = True
        self.resetTransform()
        
        pixmap = QPixmap(image_path)
        self.image_item = self.scene.addPixmap(pixmap)
        
        margin = max(pixmap.width(), pixmap.height())
        self.setSceneRect(-margin, -margin, pixmap.width() + 2 * margin, pixmap.height() + 2 * margin)
        self.centerOn(self.image_item)

    def load_existing_points(self, points_data: List[Dict]):
        for box in self.boxes:
            self.scene.removeItem(box)
        self.boxes.clear()
        
        for b_data in points_data:
            box = BoundingBox(b_data["x"], b_data["y"], b_data["w"], b_data["h"], self)
            self.scene.addItem(box)
            self.boxes.append(box)

    def get_coordinates(self) -> List[Dict]:
        result = []
        for box in self.boxes:
            rect = box.rect()
            pos = box.pos()
            result.append({
                "x": rect.x() + pos.x(),
                "y": rect.y() + pos.y(),
                "w": rect.width(),
                "h": rect.height()
            })
        return result