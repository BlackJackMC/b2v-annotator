from typing import List, Dict
from PyQt6.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsRectItem, QGraphicsTextItem
from PyQt6.QtCore import Qt, QPoint, QPointF, pyqtSignal
from PyQt6.QtGui import QImage, QPen, QBrush, QColor, QPixmap, QFont, QPainter, QPolygonF, QPainterPath, QKeySequence

class BoundingBox(QGraphicsRectItem):
    def __init__(self, x, y, w, h, canvas):
        super().__init__(x, y, w, h)
        self.canvas = canvas
        self.setPen(QPen(QColor("yellow"), 2))
        self.setBrush(QBrush(QColor(255, 255, 0, 40)))
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        
        self.mode = "none"
        self.edge = None
        self.drag_start_pos = None
        self.original_rect = None
        self.original_item_pos = None

    def get_edge_at(self, pos):
        rect = self.rect()
        margin = 8
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        
        left = abs(pos.x() - x) <= margin
        right = abs(pos.x() - (x + w)) <= margin
        top = abs(pos.y() - y) <= margin
        bottom = abs(pos.y() - (y + h)) <= margin
        
        inside_x = x + margin < pos.x() < x + w - margin
        inside_y = y + margin < pos.y() < y + h - margin
        
        if top and left: return "top-left"
        if top and right: return "top-right"
        if bottom and left: return "bottom-left"
        if bottom and right: return "bottom-right"
        if top and inside_x: return "top"
        if bottom and inside_x: return "bottom"
        if left and inside_y: return "left"
        if right and inside_y: return "right"
        return None

    def hoverMoveEvent(self, event):
        edge = self.get_edge_at(event.pos())
        if edge in ("top", "bottom"):
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif edge in ("left", "right"):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif edge in ("top-left", "bottom-right"):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif edge in ("top-right", "bottom-left"):
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        else:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.setSelected(not self.isSelected())
                event.accept()
                return
            if not self.isSelected():
                self.scene().clearSelection()
                self.setSelected(True)
            edge = self.get_edge_at(event.pos())
            self.drag_start_pos = event.scenePos()
            self.original_rect = self.rect()
            self.original_item_pos = self.pos()
            
            if edge:
                self.mode = "resizing"
                self.edge = edge
            else:
                self.mode = "moving"
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.mode == "moving":
            delta = event.scenePos() - self.drag_start_pos
            self.setPos(self.original_item_pos + delta)
            event.accept()
            return
            
        elif self.mode == "resizing":
            delta = event.scenePos() - self.drag_start_pos
            r = self.original_rect
            x, y, w, h = r.x(), r.y(), r.width(), r.height()
            
            dx = delta.x()
            dy = delta.y()
            
            if "left" in self.edge:
                new_w = w - dx
                if new_w > 10:
                    x += dx
                    w = new_w
            if "right" in self.edge:
                new_w = w + dx
                if new_w > 10:
                    w = new_w
            if "top" in self.edge:
                new_h = h - dy
                if new_h > 10:
                    y += dy
                    h = new_h
            if "bottom" in self.edge:
                new_h = h + dy
                if new_h > 10:
                    h = new_h
                    
            self.setRect(x, y, w, h)
            event.accept()
            return
            
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.mode = "none"
        self.edge = None
        super().mouseReleaseEvent(event)

class ImageCanvas(QGraphicsView):
    message = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.boxes = []
        self.image_item = None
        self.is_editable = False
        self.crop_offset_x = 0
        self.crop_offset_y = 0
        self.copied_boxes = []
        self.copied_image_size = None
        self.image_size = None
        self.setToolTip("Ctrl+A: select all boxes. Ctrl+C: copy selection. Ctrl+V: paste at the original image coordinates.")
        
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
        if not self.is_editable: return
        zoom_in_factor = 1.15
        zoom_out_factor = 1 / zoom_in_factor
        zoom_factor = zoom_in_factor if event.angleDelta().y() > 0 else zoom_out_factor
        self.scale(zoom_factor, zoom_factor)

    def mousePressEvent(self, event):
        if not self.is_editable: return

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

    def keyPressEvent(self, event):
        if not self.is_editable:
            return super().keyPressEvent(event)
        if event.matches(QKeySequence.StandardKey.SelectAll):
            for box in self.boxes:
                box.setSelected(True)
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Copy):
            selected = [coords for box, coords in zip(self.boxes, self.get_coordinates()) if box.isSelected()]
            if selected and not self._is_drawing:
                self.copied_boxes = selected
                self.copied_image_size = self.image_size
                self.message.emit(f"Copied {len(selected)} boxes.")
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            self.paste_boxes()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            selected_items = self.scene.selectedItems()
            for item in selected_items:
                if isinstance(item, BoundingBox):
                    self.boxes.remove(item)
                    self.scene.removeItem(item)
            return
        super().keyPressEvent(event)

    def paste_boxes(self):
        if not self.copied_boxes or self._is_drawing:
            return
        if self.image_size != self.copied_image_size:
            self.message.emit("Cannot paste: image dimensions differ. Coordinates were not changed.")
            return
        rect = self.image_item.boundingRect()
        for box in self.copied_boxes:
            x, y = box["x"] - self.crop_offset_x, box["y"] - self.crop_offset_y
            if x < 0 or y < 0 or x + box["w"] > rect.width() or y + box["h"] > rect.height():
                self.message.emit("Cannot paste: a box lies outside this crop. No boxes were added.")
                return
        self.scene.clearSelection()
        for data in self.copied_boxes:
            box = BoundingBox(data["x"] - self.crop_offset_x, data["y"] - self.crop_offset_y, data["w"], data["h"], self)
            self.scene.addItem(box)
            self.boxes.append(box)
            box.setSelected(True)
        self.message.emit(f"Pasted {len(self.copied_boxes)} boxes at their original image coordinates.")

    def show_empty_state(self):
        self.scene.clear()
        self.image_item = None
        self.boxes.clear()
        self.is_editable = False
        self.crop_offset_x = 0
        self.crop_offset_y = 0
        self.resetTransform()
        
        text_item = QGraphicsTextItem("No more images available")
        text_item.setFont(QFont("Arial", 24, QFont.Weight.Bold))
        text_item.setDefaultTextColor(QColor("gray"))
        text_rect = text_item.boundingRect()
        text_item.setPos(-text_rect.width() / 2, -text_rect.height() / 2)
        self.scene.addItem(text_item)
        self.setSceneRect(text_item.boundingRect())

    def load_image(self, image_path, crop_coords=None):
        self.scene.clear()
        self.boxes.clear()
        self.is_editable = True
        self.resetTransform()
        self.crop_offset_x = 0
        self.crop_offset_y = 0
        
        pre_cropped = isinstance(image_path, dict) and image_path.get("pre_cropped")
        if pre_cropped:
            pixmap = QPixmap.fromImage(image_path["image"])
            self.image_size = tuple(image_path["full_size"])
            self.crop_offset_x, self.crop_offset_y = image_path["offset"]
        else:
            pixmap = QPixmap.fromImage(image_path) if isinstance(image_path, QImage) else QPixmap(image_path)
            self.image_size = (pixmap.width(), pixmap.height())

        if not pre_cropped and crop_coords and len(crop_coords) == 4:
            points = [QPointF(p["x"], p["y"]) for p in crop_coords]
            polygon = QPolygonF(points)
            
            path = QPainterPath()
            path.addPolygon(polygon)
            
            masked_pixmap = QPixmap(pixmap.size())
            masked_pixmap.fill(Qt.GlobalColor.transparent)
            
            painter = QPainter(masked_pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipPath(path)
            painter.drawPixmap(0, 0, pixmap)
            painter.end()
            
            bounding_rect = polygon.boundingRect().toRect() 
            image_rect = pixmap.rect()
            intersected_rect = bounding_rect.intersected(image_rect)
            
            self.crop_offset_x = intersected_rect.x()
            self.crop_offset_y = intersected_rect.y()
            
            pixmap = masked_pixmap.copy(intersected_rect)

        self.image_item = self.scene.addPixmap(pixmap)
        margin = max(pixmap.width(), pixmap.height())
        self.setSceneRect(-margin, -margin, pixmap.width() + 2 * margin, pixmap.height() + 2 * margin)
        self.centerOn(self.image_item)

    def load_existing_points(self, points_data: List[Dict]):
        for box in self.boxes:
            self.scene.removeItem(box)
        self.boxes.clear()
        
        for b_data in points_data:
            box = BoundingBox(b_data["x"] - self.crop_offset_x, b_data["y"] - self.crop_offset_y, b_data["w"], b_data["h"], self)
            self.scene.addItem(box)
            self.boxes.append(box)

    def get_coordinates(self) -> List[Dict]:
        result = []
        for box in self.boxes:
            rect = box.rect()
            pos = box.pos()
            result.append({
                "x": rect.x() + pos.x() + self.crop_offset_x,
                "y": rect.y() + pos.y() + self.crop_offset_y,
                "w": rect.width(),
                "h": rect.height()
            })
        return result
