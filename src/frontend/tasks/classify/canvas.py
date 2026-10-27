from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import QGraphicsScene, QGraphicsView


class ImageCanvas(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.image_item = None
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(self.ViewportAnchor.AnchorUnderMouse)
        self.setDragMode(self.DragMode.ScrollHandDrag)

    def load_image(self, image):
        self.scene().clear()
        self.image_item = self.scene().addPixmap(QPixmap.fromImage(image))
        self.scene().setSceneRect(QRectF(0, 0, image.width(), image.height()))
        self.fit_image()

    def show_empty_state(self):
        self.scene().clear()
        self.image_item = None
        self.resetTransform()
        item = self.scene().addText("No available regions")
        item.setDefaultTextColor(Qt.GlobalColor.gray)
        self.scene().setSceneRect(item.boundingRect())

    def fit_image(self):
        if self.image_item is not None:
            self.fitInView(self.image_item, Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event):
        if self.image_item is None:
            return
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        if 0.05 <= self.transform().m11() * factor <= 50:
            self.scale(factor, factor)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_image()
