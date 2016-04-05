#! /usr/bin/env python

""" The MIT License (MIT)

    Copyright (c) 2016 Kyle Hollins Wray, University of Massachusetts

    Permission is hereby granted, free of charge, to any person obtaining a copy of
    this software and associated documentation files (the "Software"), to deal in
    the Software without restriction, including without limitation the rights to
    use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
    the Software, and to permit persons to whom the Software is furnished to do so,
    subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
    FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
    COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
    IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
    CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""


import os
import sys

thisFilePath = os.path.dirname(os.path.realpath(__file__))

sys.path.append(os.path.join(thisFilePath, "..", "..", "libnova", "python"))
import nova.pomdp_alpha_vectors as pav

import rospy

from tf.transformations import euler_from_quaternion

from std_msgs.msg import Empty
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from kobuki_msgs.msg import BumperEvent

from sigma.msg import *
from sigma.srv import *

import math
import numpy as np


class SigmaActionKobuki(object):
    """ A class to control a Kobuki following a POMDP policy. """

    def __init__(self):
        """ The constructor for the SigmaActionKobuki class. """

        # These are the world-frame x, y, and theta values of the Kobuki. They
        # are updated as it moves toward the goal.
        self.x = 0.0
        self.y = 0.0
        self.theta = 0.0

        self.previousX = 0.0
        self.previousY = 0.0
        self.previousTheta = 0.0

        # Again, these are world-frame goal values. Once it arrives at the goal, it will
        # get a new action, which assigns a new goal.
        self.atGoal = False
        self.goalX = 0.0
        self.goalY = 0.0
        self.goalTheta = 0.0

        # These are relative-frame goal values received from a 'get_action' call. This node
        # passes this information back to the POMDP, which resolves what action it was.
        self.relGoalX = 0.0
        self.relGoalY = 0.0
        self.relGoalTheta = 0.0

        # Over time, the POMDP's messages will do single-action relGoalTheta assignments. These
        # need to be tracked and accounted for over time as a permanent theta adjustment.
        self.permanentThetaAdjustment = 0.0

        # This bump variable is assigned in the callback for the sensor. It is used in
        # the odometry callback to control behavior.
        self.detectedBump = False

        # Setup the topics for the important services.
        sigmaPOMDPNamespace = rospy.get_param("~sigma_pomdp_namespace", "/sigma_pomdp_node")
        self.subModelUpdateTopic = sigmaPOMDPNamespace + "/model_update"
        self.srvGetActionTopic = sigmaPOMDPNamespace + "/get_action"
        self.srvGetBeliefTopic = sigmaPOMDPNamespace + "/get_belief"
        self.srvUpdateBeliefTopic = sigmaPOMDPNamespace + "/update_belief"

        # The distance at which we terminate saying that we're at the goal,
        # in meters and radians, respectively.
        self.atPositionGoalThreshold = rospy.get_param("~at_position_goal_threshold", 0.05)
        self.atThetaGoalThreshold = rospy.get_param("~at_theta_goal_threshold", 0.05)

        # PID control variables.
        self.pidDerivator = 0.0
        self.pidIntegrator = 0.0
        self.pidIntegratorBounds = rospy.get_param("~pid_integrator_bounds", 0.05)

        # Load the gains for PID control.
        self.pidThetaKp = rospy.get_param("~pid_theta_Kp", 1.0)
        self.pidThetaKi = rospy.get_param("~pid_theta_Ki", 0.2)
        self.pidThetaKd = rospy.get_param("~pid_theta_Kd", 0.2)

        self.desiredVelocity = rospy.get_param("~desired_velocity", 0.2)

        # Finally, we create variables for the messages.
        self.started = False
        self.resetRequired = False

        self.subKobukiOdom = None
        self.subKobukiBump = None
        self.pubKobukiVel = None
        self.pubKobukiResetOdom = None

    def start(self):
        """ Start the necessary messages to operate the Kobuki. """

        if self.started:
            rospy.logwarn("Warn[SigmaActionKobuki.start]: Already started.")
            return

        #rospy.sleep(15)

        self.subModelUpdate = rospy.Subscriber(self.subModelUpdateTopic,
                                              ModelUpdate,
                                              self.sub_model_update)

        subKobukiOdomTopic = rospy.get_param("~sub_kobuki_odom", "/odom")
        self.subKobukiOdom = rospy.Subscriber(subKobukiOdomTopic,
                                              Odometry,
                                              self.sub_kobuki_odom)

        subKobukiBumpTopic = rospy.get_param("~sub_kobuki_bump", "/evt_bump")
        self.subKobukiBump = rospy.Subscriber(subKobukiBumpTopic,
                                              BumperEvent,
                                              self.sub_kobuki_bump)

        pubKobukiVelTopic = rospy.get_param("~pub_kobuki_vel", "/cmd_vel")
        self.pubKobukiVel = rospy.Publisher(pubKobukiVelTopic, Twist, queue_size=32)

        pubKobukiResetOdomTopic = rospy.get_param("~pub_kobuki_reset_odom", "/cmd_reset_odom")
        self.pubKobukiResetOdom = rospy.Publisher(pubKobukiResetOdomTopic, Empty, queue_size=32)

        self.started = True

    def reset(self):
        """ Reset all of the variables that change as the robot moves. """

        self.x = 0.0
        self.y = 0.0
        self.theta = 0.0

        # Note: We do *not* reset these on a 'reset'. The odometers do not get reset, and these
        # are used to compute the delta and update (x, y, theta) above according to this difference.
        # Thus, we just leave these at whatever they are, all the time.
        #self.previousX = 0.0
        #self.previousY = 0.0
        #self.previousTheta = 0.0

        self.atGoal = False
        self.goalX = 0.0
        self.goalY = 0.0
        self.goalTheta = 0.0

        self.relGoalX = 0.0
        self.relGoalY = 0.0
        self.relGoalTheta = 0.0

        self.permanentThetaAdjustment = 0.0

        self.detectedBump = False

        self.pidDerivator = 0.0
        self.pidIntegrator = 0.0

        self.started = False

        # Reset the robot's odometry.
        #if self.pubKobukiResetOdom is not None:
        #    self.pubKobukiResetOdom.publish(Empty())

        # Stop the robot's motion.
        if self.pubKobukiVel is not None: 
            control = Twist()
            self.pubKobukiVel.publish(control)

        self.resetRequired = False

    def sub_model_update(self, msg):
        """ The POMDP model has changed. We need to reset everything.

            Parameters:
                msg     --  The ModelUpdate message data.
        """

        rospy.loginfo("Info[SigmaActionKobuki.sub_model_update]: The model has been updated. Resetting.")

        self.resetRequired = True

    def sub_kobuki_odom(self, msg):
        """ Move the robot based on the SigmaPOMDP node's action.

            This gets the current action, moves the robot, calls the update belief service,
            gets the next action upon arriving at the next location, etc. It does not
            handle interrupts via a bump. It updates belief when not observing an obstacle.

            Parameters:
                msg     --  The Odometry message data.
        """

        if self.resetRequired:
            self.reset()

        if self.check_reached_goal(msg):
            self.move_to_goal(msg)

    def check_reached_goal(self, msg):
        """ Handle checking and reaching a goal.

            This means getting a new action from the SimgaPOMDP and setting variables,
            as well as doing distance calculations.

            Parameters:
                msg     --  The Odometry message data.

            Returns:
                True if successful and movement should be performed; False otherwise.
        """

        # Compute the distance to the goal given the positions, as well as the theta goal.
        errorX = self.goalX - self.x
        errorY = self.goalY - self.y
        distanceToPositionGoal = math.sqrt(pow(errorX, 2) + pow(errorY, 2))
        distanceToThetaGoal = abs(self.goalTheta - self.theta)

        print("ERROR ---- %.4f %.4f" % (errorX, errorY))

        # If the robot is far from the goal, with no bump detected either, then do nothing.
        if (distanceToPositionGoal >= self.atPositionGoalThreshold or \
                (self.relGoalX == 0.0 and self.relGoalY == 0.0 and \
                    distanceToThetaGoal >= self.atThetaGoalThreshold)) and \
                not self.detectedBump:
            return True

        # However, if it is close enough to the goal, then update the belief with
        # observing a bump or not. This may fail if not enough updates have been performed.
        rospy.wait_for_service(self.srvUpdateBeliefTopic)
        try:
            srvUpdateBelief = rospy.ServiceProxy(self.srvUpdateBeliefTopic, UpdateBelief)
            res = srvUpdateBelief(self.relGoalX, self.relGoalY, self.detectedBump)
            if not res.success:
                rospy.logwarn("Error[SigmaActionKobuki.check_reached_goal]: Failed to update belief.")
                return False
        except rospy.ServiceException:
            rospy.logerr("Error[SigmaActionKobuki.check_reached_goal]: Service exception when updating belief.")
            return False

        # Now do a service request for the SigmaPOMDP to send the current action.
        rospy.wait_for_service(self.srvGetActionTopic)
        try:
            srvGetAction = rospy.ServiceProxy(self.srvGetActionTopic, GetAction)
            res = srvGetAction()
        except rospy.ServiceException:
            rospy.logerr("Error[SigmaActionKobuki.check_reached_goal]: Service exception when getting action.")
            return False

        # This may fail if not enough updates have been performed.
        if not res.success:
            rospy.loginfo("Error[SigmaActionKobuki.check_reached_goal]: No action was returned.")
            return False

        # The new 'origin' is the current pose estimates from the odometers.
        self.x += msg.pose.pose.position.x - self.previousX
        self.y += msg.pose.pose.position.y - self.previousY
        roll, pitch, yaw = euler_from_quaternion([msg.pose.pose.orientation.x,
                                                  msg.pose.pose.orientation.y,
                                                  msg.pose.pose.orientation.z,
                                                  msg.pose.pose.orientation.w])
        self.theta += yaw - self.previousTheta

        self.previousX = msg.pose.pose.position.x
        self.previousY = msg.pose.pose.position.y
        self.previousTheta = yaw

        #rospy.logwarn("Action: [%.1f, %.1f, %.3f]" % (res.goal_x, res.goal_y, res.goal_theta))
        self.relGoalX = res.goal_x
        self.relGoalY = res.goal_y
        self.relGoalTheta = res.goal_theta

        self.permanentThetaAdjustment += self.relGoalTheta

        # Importantly, we rotate the relative goal by the relative theta provided!
        xyLength = math.sqrt(pow(self.relGoalX, 2) + pow(self.relGoalY, 2))
        xyTheta = math.atan2(self.relGoalY, self.relGoalX)
        relGoalAdjustedX = xyLength * math.cos(xyTheta + self.permanentThetaAdjustment)
        relGoalAdjustedY = xyLength * math.sin(xyTheta + self.permanentThetaAdjustment)

        # We need to translate the goal location given by srvGetAction to the world-frame.
        # They are provided as a relative goal. Theta, however, is given in 'world-frame'
        # kinda, basically because it is not in the SigmaPOMDP's state space.
        self.goalX = self.x + relGoalAdjustedX + errorX
        self.goalY = self.y + relGoalAdjustedY + errorY
        self.goalTheta = np.arctan2(self.goalY - self.y, self.goalX - self.x)

        # Finally, reset the bump detection because we have already incorporated that
        # in the belief update above.
        self.detectedBump = False

        return True

    def move_to_goal(self, msg):
        """ Move toward the goal using the relevant Kobuki messages.

            Parameters:
                msg     --  The Odometry message data.

            Returns:
                True if successful; False otherwise.
        """

        # Get the updated location and orientation from the odometry message.
        self.x += msg.pose.pose.position.x - self.previousX
        self.y += msg.pose.pose.position.y - self.previousY
        roll, pitch, yaw = euler_from_quaternion([msg.pose.pose.orientation.x,
                                                  msg.pose.pose.orientation.y,
                                                  msg.pose.pose.orientation.z,
                                                  msg.pose.pose.orientation.w])
        self.theta += yaw - self.previousTheta

        self.previousX = msg.pose.pose.position.x
        self.previousY = msg.pose.pose.position.y
        self.previousTheta = yaw

        print("ODOMETERS: %.3f %.3f %.3f" % (self.x, self.y, self.theta))

        rospy.logwarn("[x, y, theta]: [%.4f, %.4f, %.4f]" % (self.x, self.y, self.theta))
        rospy.logwarn("[goalX, goalY]: [%.4f, %.4f]" % (self.goalX, self.goalY))
        rospy.logwarn("[relGoalX, relGoalY]: [%.4f, %.4f]" % (self.relGoalX, self.relGoalY))

        control = Twist()

        # If close to the goal, then do nothing. Otherwise, drive based on normal control. However,
        # we only update the distance if there is no more relative theta adjustment required.
        distanceToPositionGoal = math.sqrt(pow(self.x - self.goalX, 2) +
                                           pow(self.y - self.goalY, 2))
        if distanceToPositionGoal < self.atPositionGoalThreshold:
            control.linear.x = 0.0
        else:
            # This assigns the desired set-point for speed in meters per second.
            control.linear.x = self.desiredVelocity

        #rospy.logwarn("Distance to Goal: %.4f" % (distanceToPositionGoal))

        # Compute the new goal theta based on the updated (noisy) location of the robot.
        self.goalTheta = np.arctan2(self.goalY - self.y, self.goalX - self.x)

        #rospy.logwarn("Goal Theta: %.4f" % (self.goalTheta))

        error = self.goalTheta - self.theta
        if error > math.pi:
            self.goalTheta -= 2.0 * math.pi
            error -= 2.0 * math.pi
        if error < -math.pi:
            self.goalTheta += 2.0 * math.pi
            error += 2.0 * math.pi

        #rospy.logwarn("Theta Error: %.4f" % (abs(error)))

        if abs(error) < self.atThetaGoalThreshold:
            control.angular.z = 0.0
        else:
            valP = error * self.pidThetaKp

            self.pidIntegrator += error
            self.pidIntegrator = np.clip(self.pidIntegrator,
                                         -self.pidIntegratorBounds,
                                         self.pidIntegratorBounds)
            valI = self.pidIntegrator * self.pidThetaKi

            self.pidDerivator = error - self.pidDerivator
            self.pidDerivator = error
            valD = self.pidDerivator * self.pidThetaKd

            # This assigns the desired set-point for relative angle.
            control.angular.z = valP + valI + valD

        self.pubKobukiVel.publish(control)

        return True

    def sub_kobuki_bump(self, msg):
        """ This method checks for sensing a bump.

            Parameters:
                msg     --  The BumperEvent message data.
        """

        self.detectedBump = (msg.state == BumperEvent.PRESSED)
