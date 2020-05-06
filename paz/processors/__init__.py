from .detection import *
from .image import *
from .draw import *
from .geometric import *
from .standard import *
from .keypoints import *
from .pose import *
from ..backend.image.opencv_image import RGB2BGR
from ..backend.image.opencv_image import BGR2RGB
from ..backend.image.opencv_image import RGB2GRAY
from ..backend.image.opencv_image import RGB2HSV
from ..backend.image.opencv_image import HSV2RGB

TRAIN = 0
VAL = 1
TEST = 2
