#!/usr/bin/python

import tf
import rospy
import actionlib
import geometry_msgs

from move_base.msg import MoveBaseAction, MoveBaseGoal

from db_planning.msg import SequenceRequestAction, SequenceRequestGoal
from db_planning.msg import ChassisAction, ChassisGoal
from db_planning.msg import FrontLoaderAction, FrontLoaderGoal
from db_planning.sequence import Sequence

class CentralPlanning:
    """
    Class definition for central_planning ROS node.
    """

    def __init__(self):
        """
        Initializes ROS and necessary services and publishers.
        """

        rospy.init_node(
            "central_planning"
            # disable_signals=True
            # log_level=rospy.DEBUG
        )

        self.insert_sequence_path = rospy.get_param("~insert_sequence_path", "./insert.csv")
        self.extract_sequence_path = rospy.get_param("~extract_sequence_path", "./extract.csv")
        self.insert_sequence = Sequence(self.insert_sequence_path)
        self.extract_sequence = Sequence(self.extract_sequence_path)
        self.action_types = ["insert", "extract"]

        self.chassis_action_name = rospy.get_param("~chassis_action_name", "chassis_actions")
        self.front_loader_action_name = rospy.get_param("~front_loader_action_name", "front_loader_actions")

        # create tag watching tf_listener
        self.tag_name = rospy.get_param("~tag_name", "tag")  # tag's TF name
        self.base_start_name = rospy.get_param("~base_start_name", "base_start_link")
        self.tag_tf_name = "/" + self.tag_name
        self.base_start_tf_name = "/" + self.base_start_name
        self.tf_listener = tf.TransformListener()

        self.saved_start_pos = None
        self.saved_start_quat = None
        self.last_saved_time = rospy.Time.now()

        # create base_move action server
        self.sequence_server = actionlib.SimpleActionServer("sequence_request", SequenceRequestAction, self.sequence_callback)
        self.sequence_server.start()

        # chassis and front loader services
        self.chassis_action = actionlib.SimpleActionClient(self.chassis_action_name, ChassisAction)
        self.chassis_action.wait_for_server()

        self.front_loader_action = actionlib.SimpleActionClient(self.front_loader_action_name, FrontLoaderAction)
        self.front_loader_action.wait_for_server()

        # wait for action client
        self.move_action_client = actionlib.SimpleActionClient("move_base/goal", MoveBaseAction)
        self.move_action_client.wait_for_server()

    ### CALLBACK FUNCTIONS ###

    def sequence_callback(self, goal):
        # transform pose from robot frame to front loader frame
        action_type = goal.action_type
        result = SequenceRequestGoal()

        if action_type not in self.action_types:
            rospy.logwarn("%s not an action type: %s" % (action_type, self.action_types))
            result.success = False
            self.sequence_server.set_aborted(result)
            return

        if self.saved_start_pos is None or self.saved_start_quat is None:
            rospy.loginfo("Saved position is null. Waiting 30s for a tag TF to appear.")
            self.tf_listener.waitForTransform("/map", self.base_start_tf_name, rospy.Time(), rospy.Duration(30.0))
            if self.saved_start_pos is None or self.saved_start_quat is None:
                rospy.logwarn("No tag visible or in memory. Can't direct the robot")
                result.success = False
                self.sequence_server.set_aborted(result)
                return

        # Creates a goal to send to the action server.
        pose_base_link = self.get_sequence_start_pose("/map")

        goal = MoveBaseGoal()
        goal.target_pose.header.frame_id = "map"
        goal.target_pose.pose = pose_base_link

        # Sends the goal to the action server.
        self.move_action_client.send_goal(goal)

        # Waits for the server to finish performing the action.
        self.move_action_client.wait_for_result()

        # Get the result of executing the action
        result = self.move_action_client.get_result()

        print result
        if not result:
            rospy.logwarn("move_base failed to direct the robot to the directed position")
            result.success = False
            self.sequence_server.set_aborted(result)
            return

        # TODO: pause rtabmap if running
        try:
            if action_type == "insert":
                seq_result = self.insert_action_sequence()
            elif action_type == "extract":
                seq_result = self.extract_action_sequence()
            else:
                raise RuntimeError("Invalid action type: %s" % action_type)
        except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException) as e:
            rospy.logwarn("Failed to complete action sequence due to TF error. Is the tag in view?: %s" % str(e))
            result.success = False
            self.sequence_server.set_aborted(result)
            return
        except BaseException as e:
            rospy.logwarn("Failed to complete action sequence: %s" % str(e))
            result.success = False
            self.sequence_server.set_aborted(result)
            return
        # TODO: resume rtabmap

        if seq_result is None:
            result.success = True
            self.sequence_server.set_succeeded(result)
        else:
            rospy.logwarn("Failed to complete action sequence. Stopped at index #%s" % seq_result)
            result.success = False
            self.sequence_server.set_aborted(result)

    def insert_action_sequence(self):
        adj_sequence = self.adjust_sequence_into_odom(self.insert_sequence)
        for index, action in enumerate(adj_sequence):
            rospy.loginfo("Running insert action #%s" % index)
            result = self.run_action(action)
            if not result:
                return index
        return None

    def extract_action_sequence(self, start_pose):
        adj_sequence = self.adjust_sequence_into_odom(self.extract_sequence)
        for index, action in enumerate(adj_sequence):
            rospy.loginfo("Running extract action #%s" % index)
            result = self.run_action(action)
            if not result:
                return index
        return None

    def run_action(self, action):
        rospy.loginfo("Sending action %s" % str(action))
        chassis_goal = self.get_goal_msg(ChassisGoal, action)
        front_loader_goal = self.get_goal_msg(FrontLoaderGoal, action)

        self.chassis_action.send_goal_async(chassis_goal, feedback_callback=self.chassis_action_progress)
        self.front_loader_action.send_goal_async(front_loader_goal, feedback_callback=self.front_loader_action_progress)

        self.front_loader_action.wait_for_result()
        rospy.loginfo("Front loader result received")

        self.chassis_action.wait_for_result()
        rospy.loginfo("Chassis result received")

        chassis_result = self.chassis_action.get_result()
        front_loader_result = self.front_loader_action.get_result()

        rospy.loginfo("Front loader result: %s" % front_loader_result.success)
        rospy.loginfo("Chassis result: %s" % chassis_result.success)

        if chassis_result.success and front_loader_result.success:
            return True
        else:
            return False

    def chassis_action_progress(self, msg):
        rospy.loginfo("x: %s, y: %s" % (msg.current_x, msg.current_y))

    def front_loader_action_progress(self, msg):
        rospy.loginfo("z: %s" % (msg.current_z))

    def get_goal_msg(self, goal_msg_class, action):
        goal_msg = goal_msg_class()
        for key, value in action.items():
            setattr(goal_msg, key, value)
        return goal_msg

    def get_sequence_start_pose(self, frame_id):
        pose = geometry_msgs.msg.PoseStamped()
        pose.header.frame_id = self.tag_tf_name
        pose.pose.position.x = self.saved_start_pos[0]
        pose.pose.position.y = self.saved_start_pos[1]
        pose.pose.position.z = self.saved_start_pos[2]
        pose.pose.orientation.w = self.saved_start_quat[3]
        pose.pose.orientation.x = self.saved_start_quat[0]
        pose.pose.orientation.y = self.saved_start_quat[1]
        pose.pose.orientation.z = self.saved_start_quat[2]

        tfd_pose = self.tf_listener.transformPose(frame_id, pose)
        return tfd_pose

    def adjust_sequence_into_odom(self, sequence):
        # adjust all actions while the tag is in view. If it's not, throw an error
        adj_sequence = []
        for action in sequence:
            adj_sequence.append(self.get_action_goal_in_odom(action))
        return adj_sequence

    def get_action_goal_in_odom(self, action):
        # all action coordinates are relative to the action_start_link frame
        # and need to be transformed into the odom frame so goals can be sent.

        # The robot's starting position isn't needed since we're computing from
        # the theoretically perfect action_start_link which the robot should
        # be very close to

        pose = geometry_msgs.msg.PoseStamped()
        pose.header.frame_id = self.base_start_tf_name
        pose.pose.position.x = action["goal_x"]
        pose.pose.position.y = action["goal_y"]
        pose.pose.position.z = 0.0

        if math.isnan(action["goal_angle"])
            pose.pose.orientation.x = 0.0
            pose.pose.orientation.y = 0.0
            pose.pose.orientation.z = 0.0
            pose.pose.orientation.w = 1.0
        else:
            action_quaternion = tf.transformations.quaternion_from_euler(0.0, 0.0, action["goal_angle"])
            pose.pose.orientation.x = action_quaternion[0]
            pose.pose.orientation.y = action_quaternion[1]
            pose.pose.orientation.z = action_quaternion[2]
            pose.pose.orientation.w = action_quaternion[3]

        tfd_pose = self.tf_listener.transformPose("/odom", pose)

        tfd_action = {}
        tfd_action.update(action)
        tfd_action["goal_x"] = tfd_pose.pose.position.x
        tfd_action["goal_y"] = tfd_pose.pose.position.y
        # TBD: does goal_z need to be adjusted?

        if not math.isnan(action["goal_angle"]):
            tfd_goal_angle = tf.transformations.euler_from_quaternion(
                tfd_pose.pose.orientation.x,
                tfd_pose.pose.orientation.y,
                tfd_pose.pose.orientation.z,
                tfd_pose.pose.orientation.w
            )[2]
            tfd_action["goal_angle"] = tfd_goal_angle
            # if the original goal_angle is NaN, that value will be copied over

        return tfd_action

    def run(self):
        rate = rospy.Rate(10.0)
        while not rospy.is_shutdown():
            try:
                # if self.tf_listener.canTransform("/map", self.base_start_tf_name, rospy.Time(0)):
                trans, rot = self.tf_listener.lookupTransform("/map", self.base_start_tf_name, rospy.Time(0))
                self.saved_start_pos = trans
                self.saved_start_quat = rot
                self.last_saved_time = rospy.Time.now()
            except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
                continue

            if rospy.Time.now() - self.last_saved_time > rospy.Time.Duration(300.0):
                rospy.loginfo("5 minutes elapsed. Saved tag position erased.")
                self.saved_start_pos = None
                self.saved_start_quat = None
            rate.sleep()

if __name__ == "__main__":
    try:
        node = CentralPlanning()
        node.run()

    except rospy.ROSInterruptException:
        pass

    finally:
        rospy.loginfo("Exiting central_planning node")
