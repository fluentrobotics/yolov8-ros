from typing import Optional

import cv2
import numpy as np
import torch
from ultralytics import YOLO
from ultralytics.engine.results import Results

import rospy
from cv_bridge import CvBridge
from geometry_msgs.msg import PointStamped
from sensor_msgs.msg import CameraInfo, CompressedImage, Image
from visualization_msgs.msg import Marker

from yolov8_ros import logger, get_model_download_dir


class YoloSkeletonRightWristNode:
    def __init__(self) -> None:
        self._model = YOLO(get_model_download_dir() / "yolov8x-pose.pt")

        #####################
        # ROS-related setup #
        #####################
        # Subscribe to the uncompressed image since there seems to be some
        # difficulty subscribing to the /compressedDepth topic.
        depth_topic: str = rospy.get_param(
            "depth_topic", default="/camera/aligned_depth_to_color/image_raw"
        )
        rgb_topic: str = rospy.get_param(
            "rgb_topic", default="/camera/color/image_raw/compressed"
        )
        camera_info_topic: str = rospy.get_param(
            "camera_info_topic", default="/camera/aligned_depth_to_color/camera_info"
        )
        self._stretch_robot_rotate_image_90deg: bool = rospy.get_param(
            "stretch_robot_rotate_image_90deg", default=True
        )

        self._wrist_position_pub = rospy.Publisher(
            "/yolov8/pose/right_wrist", PointStamped, queue_size=1
        )
        self._debug_marker_pub = rospy.Publisher(
            "/yolov8/markers/right_wrist", Marker, queue_size=1
        )
        self._debug_img_pub = rospy.Publisher(
            "/yolov8/pose/vis/compressed", CompressedImage, queue_size=1
        )

        # Perform a blocking read of a single CameraInfo message instead of
        # creating a persistent subscriber.
        logger.info("Waiting 5s for CameraInfo...")
        camera_info_msg: CameraInfo = rospy.wait_for_message(
            camera_info_topic, CameraInfo, timeout=5
        )
        self._intrinsic_matrix = np.array(camera_info_msg.K).reshape((3, 3))
        self._rgb_msg: Optional[CompressedImage] = None
        self._depth_msg: Optional[Image] = None
        self._cv_bridge = CvBridge()

        # TODO(elvout): how to we make sure these messages are synchronized?
        # Maybe track timestamps and only use timestamps with both messages
        self._rgb_sub = rospy.Subscriber(
            rgb_topic, CompressedImage, self._rgb_callback, queue_size=1
        )
        self._depth_sub = rospy.Subscriber(
            depth_topic, Image, self._depth_callback, queue_size=1
        )

    def _rgb_callback(self, msg: CompressedImage) -> None:
        self._rgb_msg = msg

    def _depth_callback(self, msg: Image) -> None:
        self._depth_msg = msg

    def _point_to_marker(self, point: PointStamped) -> Marker:
        marker = Marker()
        marker.header.frame_id = point.header.frame_id
        marker.header.stamp = rospy.Time(0)

        marker.type = Marker.ARROW
        marker.scale.x = 0.25
        marker.scale.y = 0.05
        marker.scale.z = 0.05
        marker.color.a = 1.0
        marker.color.r = 1.0
        marker.color.g = 0.0
        marker.color.b = 1.0

        marker.pose.position.x = point.point.x - marker.scale.x
        marker.pose.position.y = point.point.y
        marker.pose.position.z = point.point.z

        marker.pose.orientation.x = 0.7071068
        marker.pose.orientation.y = 0.0
        marker.pose.orientation.z = 0.0
        marker.pose.orientation.w = 0.7071068

        return marker

    def run_inference_and_publish_pose(self) -> None:
        if self._rgb_msg is None or self._depth_msg is None:
            return

        rgb_msg_header = self._rgb_msg.header

        bgr_image = self._cv_bridge.compressed_imgmsg_to_cv2(self._rgb_msg)
        depth_image = (
            self._cv_bridge.imgmsg_to_cv2(self._depth_msg).astype(np.float32) / 1000.0
        )

        if self._stretch_robot_rotate_image_90deg:
            bgr_image = cv2.rotate(bgr_image, cv2.ROTATE_90_CLOCKWISE)

        results: list[Results] = self._model.predict(bgr_image, verbose=False)

        # Consider only the first YOLOv8 detection result for now, if it exists.
        if not results:
            return
        result = results[0]

        if result.keypoints is None:
            return

        # Keypoint index reference:
        # https://docs.ultralytics.com/guides/workouts-monitoring/?h=pose#keypoints-map
        # The right wrist should be index 10 when when the camera and the human
        # are facing one another.
        right_wrist_keypoint_idx = 10

        try:
            # NOTE: Potential future bug. For some reason an empty dimension is
            # inserted at the front of the keypoints.xy tensor. If that
            # dimension already exists or ends up being used for something,
            # index 0 might be the incorrect one to use.
            right_wrist_pixel_xy = result.keypoints.xy[
                0, right_wrist_keypoint_idx
            ].cpu()
        except IndexError:
            # This error may be raised if the `xy` array is not well formed or
            # not long enough (e.g. no limbs were detected).
            return

        # The image coordinates corresponding to the right wrist may be all
        # zeros, for example if legs were detected but arms were not.
        if not torch.all(right_wrist_pixel_xy):
            return

        # Find the pixel coordinate in the un-rotated image so we can query the
        # depth image.
        if self._stretch_robot_rotate_image_90deg:
            y = bgr_image.shape[1] - right_wrist_pixel_xy[0]
            x = right_wrist_pixel_xy[1]
        else:
            x, y = right_wrist_pixel_xy

        depth = depth_image[int(y), int(x)]

        # We can't do anything without valid depth information.
        if depth == 0:
            return

        P_camera_rightwrist = (
            depth * np.linalg.inv(self._intrinsic_matrix) @ np.array([[x], [y], [1]])
        )

        point_msg = PointStamped()
        point_msg.header = rgb_msg_header
        point_msg.point.x = P_camera_rightwrist[0]
        point_msg.point.y = P_camera_rightwrist[1]
        point_msg.point.z = P_camera_rightwrist[2]
        self._wrist_position_pub.publish(point_msg)

        marker_msg = self._point_to_marker(point_msg)
        self._debug_marker_pub.publish(marker_msg)

        annotated_debug_img = result.plot()
        annotated_debug_img_msg = self._cv_bridge.cv2_to_compressed_imgmsg(
            annotated_debug_img
        )
        self._debug_img_pub.publish(annotated_debug_img_msg)


def main() -> None:
    rospy.init_node("yolov8_right_wrist")
    node = YoloSkeletonRightWristNode()

    logger.success("node initialized")

    # TODO(elvout): switch to ros service
    while not rospy.is_shutdown():
        node.run_inference_and_publish_pose()
        rospy.sleep(0.05)


if __name__ == "__main__":
    main()
