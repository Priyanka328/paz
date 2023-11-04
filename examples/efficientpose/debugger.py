import numpy as np
from paz.abstract import Processor
import paz.processors as pr
from paz.backend.image import lincolor
from efficientpose import EFFICIENTPOSEA
from processors import (DrawPose6D, ComputeSelectedIndices, ScaleBoxes2D, ToPose6D)
import argparse
import numpy as np
import cv2
from paz.backend.image import show_image
from pose import get_class_names
from paz.abstract import ProcessingSequence
from paz.processors import TRAIN, VAL
from pose import (AugmentPose, EFFICIENTPOSEA, LINEMOD_CAMERA_MATRIX,
                  LINEMOD_OBJECT_SIZES, EfficientPosePreprocess)
from linemod import LINEMOD
import numpy as np
from paz.abstract import Processor
import paz.processors as pr
from paz.backend.image import lincolor
from efficientpose import EFFICIENTPOSEA
from processors import DrawPose6D


description = 'Training script for single-shot object detection models'
parser = argparse.ArgumentParser(description=description)
parser.add_argument('-dp', '--data_path', default='Linemod_preprocessed/',
                    type=str, help='Path for writing model weights and logs')
parser.add_argument('-id', '--object_id', default='08',
                    type=str, help='ID of the object to train')
args = parser.parse_args()

data_splits = ['train', 'test']
data_names = ['LINEMOD', 'LINEMOD']

# loading datasets
data_managers, datasets, evaluation_data_managers = [], [], []
for data_name, data_split in zip(data_names, data_splits):
    data_manager = LINEMOD(args.data_path, args.object_id,
                           data_split, name=data_name)
    data_managers.append(data_manager)
    datasets.append(data_manager.load_data())
    if data_split == 'test':
        eval_data_manager = LINEMOD(
            args.data_path, args.object_id, data_split,
            name=data_name, evaluate=True)
        evaluation_data_managers.append(eval_data_manager)

num_classes = data_managers[0].num_classes

# instantiating model
model = EFFICIENTPOSEA(num_classes, base_weights='COCO', head_weights=None)

# setting data augmentation pipeline
augmentators = []
for split in [TRAIN, VAL]:
    augmentator = AugmentPose(model, split, size=512, num_classes=num_classes)
    augmentators.append(augmentator)

sequencers = []
for data, augmentator in zip(datasets, augmentators):
    sequencer = ProcessingSequence(augmentator, 1, data)
    sequencers.append(sequencer)

class DenormalizeBoxes2D(Processor):
    """Denormalizes boxes shapes to be in accordance to the original
    image size.

    # Arguments:
        image_size: List containing height and width of an image.
    """
    def __init__(self):
        super(DenormalizeBoxes2D, self).__init__()

    def call(self, image, boxes2D):
        shape = image.shape[:2]
        for box2D in boxes2D:
            box2D.coordinates = denormalize_box(box2D.coordinates, shape)
        return boxes2D


def denormalize_box(box, image_shape):
    """Scales corner box coordinates from normalized values to image dimensions

    # Arguments
        box: Numpy array containing corner box coordinates.
        image_shape: List of integers with (height, width).

    # Returns
        returns: box corner coordinates in image dimensions
    """
    x_min, y_min, x_max, y_max = box[:4]
    height, width = 512*1.25, 640
    x_min = int(x_min * width)
    y_min = int(y_min * height)
    x_max = int(x_max * width)
    y_max = int(y_max * height)
    return (x_min, y_min, x_max, y_max)


class EfficientPosePostprocess(Processor):
    """Postprocessing pipeline for EfficientPose.

    # Arguments
        model: Keras model.
        class_names: List of strings indicating class names.
        score_thresh: Float between [0, 1].
        nms_thresh: Float between [0, 1].
        variances: List of float values.
        class_arg: Int, index of the class to be removed.
        num_pose_dims: Int, number of dimensions for pose.
    """
    def __init__(self, model, class_names, score_thresh, nms_thresh,
                 variances=[0.1, 0.1, 0.2, 0.2], class_arg=None,
                 num_pose_dims=3):
        super(EfficientPosePostprocess, self).__init__()
        self.num_pose_dims = num_pose_dims
        self.postprocess_1 = pr.SequentialProcessor([
            pr.Squeeze(axis=None),
            pr.DecodeBoxes(model.prior_boxes, variances),
            pr.RemoveClass(class_names, class_arg)])
        self.scale_boxes2D = ScaleBoxes2D()
        self.postprocess_2 = pr.SequentialProcessor([
            pr.NonMaximumSuppressionPerClass(nms_thresh),
            pr.MergeNMSBoxWithClass(),
            pr.FilterBoxes(class_names, score_thresh)])
        self.to_boxes2D = pr.ToBoxes2D(class_names)
        self.round_boxes = pr.RoundBoxes2D()
        self.denormalize = DenormalizeBoxes2D()
        self.compute_selections = ComputeSelectedIndices()
        self.squeeze = pr.Squeeze(axis=0)
        self.transform_rotations = pr.Scale(np.pi)
        self.to_pose_6D = ToPose6D(class_names)

    def call(self, image, model_output, image_scale, camera_parameter):
        detections, transformations = model_output
        box_data = self.postprocess_1(detections)
        box_data_all = box_data
        box_data = self.postprocess_2(box_data)
        boxes2D = self.to_boxes2D(box_data)
        boxes2D = self.denormalize(image, boxes2D)
        boxes2D = self.scale_boxes2D(boxes2D, 1 / image_scale)
        boxes2D = self.round_boxes(boxes2D)

        rotations = transformations[:, :, :self.num_pose_dims]
        translations = transformations[:, :, self.num_pose_dims:]
        poses6D = []
        if len(boxes2D) > 0:
            selected_indices = self.compute_selections(box_data_all, box_data)
            rotations = self.squeeze(rotations)
            rotations = rotations[selected_indices]
            rotations = self.transform_rotations(rotations)
            translations = self.squeeze(translations)
            translations = translations[selected_indices]

        poses6D = self.to_pose_6D(box_data, rotations, translations)
        return boxes2D, poses6D


class DetectAndEstimateEfficientPose(Processor):
    def __init__(self, model, class_names, score_thresh, nms_thresh,
                 LINEMOD_CAMERA_MATRIX, LINEMOD_OBJECT_SIZES, preprocess=None,
                 postprocess=None, variances=[0.1, 0.1, 0.2, 0.2],
                 show_boxes2D=False, show_poses6D=True):
        self.model = model
        self.class_names = class_names
        self.score_thresh = score_thresh
        self.nms_thresh = nms_thresh
        self.variances = variances
        self.class_to_sizes = LINEMOD_OBJECT_SIZES
        self.camera_matrix = LINEMOD_CAMERA_MATRIX
        self.colors = lincolor(len(self.class_to_sizes.keys()))
        self.show_boxes2D = show_boxes2D
        self.show_poses6D = show_poses6D
        if preprocess is None:
            self.preprocess = EfficientPosePreprocess(model)
        if postprocess is None:
            self.postprocess = EfficientPosePostprocess(
                model, class_names, score_thresh, nms_thresh, class_arg=0)

        super(DetectAndEstimateEfficientPose, self).__init__()
        self.draw_boxes2D = pr.DrawBoxes2D(self.class_names)
        self.wrap = pr.WrapOutput(['image', 'boxes2D', 'poses6D'])

    def _build_draw_pose6D(self, name_to_size, camera_parameter):
        name_to_draw = {}
        iterator = zip(name_to_size.items(), self.colors)
        for (name, object_size), box_color in iterator:
            draw = DrawPose6D(object_size, camera_parameter, box_color)
            name_to_draw[name] = draw
        return name_to_draw

    def call(self, image, detections, transformations):
        preprocessed_data = self.preprocess(image)
        _, _, camera_parameter = preprocessed_data
        outputs = detections, transformations
        boxes2D, poses6D = self.postprocess(
            image, outputs, 1.0, camera_parameter)
        if self.show_boxes2D:
            image = self.draw_boxes2D(image, boxes2D)

        if self.show_poses6D:
            self.draw_pose6D = self._build_draw_pose6D(
                self.class_to_sizes, self.camera_matrix)
            for box2D, pose6D in zip(boxes2D, poses6D):
                image = self.draw_pose6D[box2D.class_name](image, pose6D)
        return self.wrap(image, boxes2D, poses6D)


class EFFICIENTPOSEALINEMODDRILLER(DetectAndEstimateEfficientPose):
    def __init__(self, score_thresh=0.60, nms_thresh=0.45,
                 show_boxes2D=False, show_poses6D=True):
        names = get_class_names('LINEMOD_EFFICIENTPOSE_DRILLER')
        model = EFFICIENTPOSEA(num_classes=len(names), base_weights='COCO',
                               head_weights=None,  momentum=0.99,
                               epsilon=0.001, activation='softmax')
        super(EFFICIENTPOSEALINEMODDRILLER, self).__init__(
            model, names, score_thresh, nms_thresh,
            LINEMOD_CAMERA_MATRIX, LINEMOD_OBJECT_SIZES,
            show_boxes2D=show_boxes2D, show_poses6D=show_poses6D)

detect = EFFICIENTPOSEALINEMODDRILLER(score_thresh=0.5, nms_thresh=0.45,
                                      show_boxes2D=True, show_poses6D=True)


def de_process_image(image):
    image = image[:384, :, :]
    image = cv2.resize(image, (640, 480))
    image = 255 * (image - image.min()) / (image.max() - image.min())
    image = image.astype(np.uint8)
    return image


seq_id = pr.TRAIN
for i in range(len(sequencers[seq_id])):
    seq = sequencers[seq_id][i]
    image = seq[0]['image'][0]
    image = de_process_image(image)
    boxes = seq[1]['boxes'][0]
    rotations = seq[1]['transformation'][0][:, :3]
    translations = seq[1]['transformation'][0][:, 6:9]
    transformation = np.concatenate((rotations, translations), axis=1)
    transformation = np.expand_dims(transformation, axis=0)
    inferences = detect(image, boxes, transformation)
    show_image(inferences['image'])
