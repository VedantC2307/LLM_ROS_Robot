#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from rclpy.action import ActionServer, CancelResponse
from robot_messages.action import MotorControl
from motor_controller.robot_control_motor import ramped_move_motors, ramped_stop_motors, MEC_STRAIGHT_FORWARD, MEC_STRAIGHT_BACKWARD, MEC_ROTATE_CLOCKWISE, MEC_ROTATE_COUNTERCLOCKWISE
from threading import Lock
from geometry_msgs.msg import Vector3

class MotorControlNode(Node):
    def __init__(self):
        super().__init__('motor_control_node')

        # ROS 2 Action Server
        self.action_server = ActionServer(
            self,
            MotorControl,
            'motor_control',
            self.execute_callback,
            cancel_callback=self.cancel_callback
        )

        # Subscribers
        self.subscription = self.create_subscription(
            Vector3,
            '/pose_data',
            self.pose_callback,
            2
        )

        # self.subscription_ultrasonic = self.create_subscription(
        #     Float32,
        #     '/ultrasonic_distance',
        #     self.distance_callback,
        #     2
        # )

        # Constants
        self.SAFE_DISTANCE_THRESHOLD = 0.20  # meters
        self.MOVEMENT_CHECK_RATE = 0.1  # seconds
      

        # State variables
        self.current_pose = None
        self.start_pose = None
        self.goal_distance = None
        self.goal_rotation = None  # goal for the rotation
        self.goal_handle = None
        self.object_distance = None
        # self.is_obstacle_detected = False
        self.is_moving = False
        self.start_roll = 0.0

        # Thread safety
        self.pose_lock = Lock()

        # Create a timer for movement monitoring
        self.movement_timer = self.create_timer(
            self.MOVEMENT_CHECK_RATE,
            self.movement_control_callback
        )

        self.get_logger().info("Motor Control Node initialized!")

    def cancel_callback(self, goal_handle):
        self.get_logger().info('Received cancel request')
        self.stop_movement()
        return CancelResponse.ACCEPT

    def stop_movement(self):
        """Safely stop movement and reset state"""
        self.is_moving = False
        ramped_stop_motors()
        self.reset_action_state()
        self.get_logger().info("Motors stopped.")

    def distance_callback(self, msg):
        try:
            self.object_distance = msg.data
            previous_obstacle_state = self.is_obstacle_detected
            self.is_obstacle_detected = self.object_distance < self.SAFE_DISTANCE_THRESHOLD

            if self.is_obstacle_detected and not previous_obstacle_state:
                self.get_logger().warn(f"Obstacle detected at {self.object_distance:.2f} meters")
                if self.is_moving:
                    self.stop_movement()
                    
        except Exception as e:
            self.get_logger().error(f"Error in ultrasonic callback: {str(e)}")
            self.is_obstacle_detected = True

    def pose_callback(self, msg):
        """Thread-safe pose update"""
        with self.pose_lock:
            self.current_pose = msg
            self.get_logger().debug(f"Position Z updated: {msg.z}")

    def execute_callback(self, goal_handle):
        """Action execution callback"""
        goal_handle.accept()

        self.get_logger().info("Received action goal")
        command = goal_handle.request.command
        distance_to_travel = goal_handle.request.distance
        rotation_angle = goal_handle.request.rotation_degrees

        # Set robot direction based on command
        if command == "MOVE_FORWARD":
            robot_direction = MEC_STRAIGHT_FORWARD
            self.get_logger().info(f"Command: Moving Forward")
        elif command == "MOVE_BACKWARD":
            robot_direction = MEC_STRAIGHT_BACKWARD
            self.get_logger().info(f"Command: Moving Backward")
        elif command == "ROTATE_CLOCKWISE":
            robot_direction = MEC_ROTATE_CLOCKWISE
            self.get_logger().info(f"Command: Rotating Clockwise")
        elif command == "ROTATE_COUNTERCLOCKWISE":
            robot_direction = MEC_ROTATE_COUNTERCLOCKWISE
            self.get_logger().info(f"Command: Rotating Counter-clockwise")
        else:
            self.get_logger().error(f"Unknown command: {command}")
            goal_handle.abort()
            return MotorControl.Result(success=False)

        # Wait for initial pose data
        if not self.wait_for_pose_data():
            self.get_logger().error("Pose data not received, aborting goal")
            goal_handle.abort()
            return MotorControl.Result(success=False)


        # Initialize movement
        with self.pose_lock:
            self.start_pose = self.current_pose
            # Set appropriate goal based on command type
            if command in ["MOVE_FORWARD", "MOVE_BACKWARD"]:
                self.goal_distance = distance_to_travel
                
                self.get_logger().info(f"Starting {command.lower()} motion, target distance: {distance_to_travel}m")
            elif command in ["ROTATE_CLOCKWISE", "ROTATE_COUNTERCLOCKWISE"]:
                self.goal_rotation = rotation_angle  # For rotation, use angle as the goal
                self.start_roll = self.current_pose.z
                self.get_logger().info(f"Starting {command.lower()} rotation, target angle: {distance_to_travel} degrees")
            
            self.goal_handle = goal_handle
            self.is_moving = True
        
        try:
            if command in ["MOVE_FORWARD", "MOVE_BACKWARD"]:
                ramped_move_motors(robot_direction)
            else:  # For rotation commands
                ramped_move_motors(robot_direction)
                
        except Exception as e:
            self.get_logger().error(f"Error moving motors: {str(e)}")
            goal_handle.abort()
            return MotorControl.Result(success=False)

        # Return immediately, let timer handle monitoring
        return MotorControl.Result(success=True)

    def movement_control_callback(self):
        """Timer callback for continuous movement monitoring"""
        if not self.is_moving or self.goal_handle is None:
            return
        
        with self.pose_lock:
            try:
                if self.current_pose is None or self.start_pose is None:
                    return
                
                command = self.goal_handle.request.command

                if command in ["MOVE_FORWARD", "MOVE_BACKWARD"]:
                    traveled_distance = self.calculate_distance(self.start_pose, self.current_pose)
                
                    # Publish feedback
                    if self.goal_handle:
                        feedback = MotorControl.Feedback()
                        feedback.status = f"Traveled: {traveled_distance:.2f} meters"
                        self.goal_handle.publish_feedback(feedback)
                        self.get_logger().info(feedback.status)

                    safe_travel_distance = traveled_distance + 0.2

                    # Check if we've reached the goal
                    if safe_travel_distance >= self.goal_distance:
                        self.get_logger().info(f"Target distance {self.goal_distance:.2f} meters reached")
                        self.stop_movement()

                        # Ensure goal_handle is still valid before marking success
                        if self.goal_handle and self.goal_handle.is_active:
                            if goal_condition_met:
                                self.goal_handle.succeed()  # Explicitly succeed
                                self.goal_handle = None
                                self.stop_movement()
                
                elif command in ["ROTATE_CLOCKWISE", "ROTATE_COUNTERCLOCKWISE"]:
                    current_roll = self.current_pose.z
                    rotated_angle = - round(current_roll - self.start_roll, 2) + 5.0

                    # Publish feedback
                    if self.goal_handle:
                        feedback = MotorControl.Feedback()
                        feedback.status = f"Rotated: {rotated_angle:.2f} degrees"
                        self.goal_handle.publish_feedback(feedback)
                        self.get_logger().info(feedback.status)

                    if rotated_angle >= round(self.goal_rotation, 1): # Absolute value for direction agnostic
                        self.get_logger().info(f"Target rotation {self.goal_rotation:.2f} degrees reached")
                        self.stop_movement()

                            # Ensure goal_handle is still valid before marking success
                        if self.goal_handle and self.goal_handle.is_active:
                            if goal_condition_met:
                                self.goal_handle.succeed()  # Explicitly succeed
                                self.goal_handle = None
                                self.stop_movement()

            except Exception as e:
                self.get_logger().error(f"Error in movement control: {str(e)}")
                self.stop_movement()
                if self.goal_handle:
                    self.goal_handle.abort()
                    self.goal_handle = None
                self.stop_movement()

    def wait_for_pose_data(self, timeout=5.0):
        """Wait for initial pose data with timeout"""
        start_time = self.get_clock().now().nanoseconds / 1e9
        while self.current_pose is None and (self.get_clock().now().nanoseconds / 1e9 - start_time < timeout):
            self.get_logger().warn("Waiting for initial pose data...")
            rclpy.spin_once(self, timeout_sec=0.1)

        if self.current_pose is None:
            self.get_logger().error("Timeout waiting for pose data")
            return False
        return True

    def calculate_distance(self, start_pose, current_pose):
        """Calculate the distance traveled along the z-axis"""
        # x_start = start_pose.x
        # x_current = current_pose.x
        # distance = round(x_current - x_start, 2)
        # Distance in meters
        return current_pose.x - start_pose.x

    def reset_action_state(self):
        """Reset all action-related state variables"""
        self.start_pose = None
        self.goal_distance = None
        self.goal_rotation = None
        self.goal_handle = None
        self.is_moving = False
        self.start_roll = 0.0

def main(args=None):
    rclpy.init(args=args)
    node = MotorControlNode()
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("Shutting down Motor Control Node")
    finally:
        node.stop_movement()
        node.destroy_node()
        rclpy.shutdown()

if __name__ == "__main__":
    main()
