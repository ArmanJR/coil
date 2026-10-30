"""The UI's kinematic projection, also available to path-aware controllers.

Rightward x, forward y, and clockwise heading are positive. Reference and car
start together at active frame 100. This is illustrative geometry, not a second
vehicle physics model or a replacement for the official acceleration score.
"""

import math


def advance_pose(pose, speed, acceleration, dt):
    x, y, heading = pose
    speed = max(0.0, float(speed))
    yaw_rate = float(acceleration) / speed if speed > 0.1 else 0.0
    end_heading = heading + yaw_rate * dt
    if abs(yaw_rate) < 1e-8:
        return (
            x + math.sin(heading) * speed * dt,
            y + math.cos(heading) * speed * dt,
            end_heading,
        )
    return (
        x + speed / yaw_rate * (math.cos(heading) - math.cos(end_heading)),
        y + speed / yaw_rate * (math.sin(end_heading) - math.sin(heading)),
        end_heading,
    )


class PathTracker:
    def __init__(self):
        self.actual = self.reference = (0.0, 0.0, 0.0)
        self.frames = 0
        self.offset_squared = 0.0
        self.max_offset = 0.0
        self.max_heading = 0.0

    def feedback(self):
        x, y, heading = self.actual
        rx, ry, reference_heading = self.reference
        return {
            "lateral_offset_m": (x - rx) * math.cos(reference_heading)
            - (y - ry) * math.sin(reference_heading),
            "heading_error_rad": math.atan2(
                math.sin(heading - reference_heading),
                math.cos(heading - reference_heading),
            ),
        }

    def update(self, speed, actual, target, dt):
        self.actual = advance_pose(self.actual, speed, actual, dt)
        self.reference = advance_pose(self.reference, speed, target, dt)
        feedback = self.feedback()
        self.frames += 1
        self.offset_squared += feedback["lateral_offset_m"] ** 2
        self.max_offset = max(self.max_offset, abs(feedback["lateral_offset_m"]))
        self.max_heading = max(self.max_heading, abs(feedback["heading_error_rad"]))

    def metrics(self):
        return {
            "path_frames": self.frames,
            "path_rmse_m": math.sqrt(self.offset_squared / self.frames)
            if self.frames
            else 0.0,
            "max_path_offset_m": self.max_offset,
            "final_path_offset_m": self.feedback()["lateral_offset_m"],
            "max_heading_error_deg": math.degrees(self.max_heading),
            "final_heading_error_deg": math.degrees(
                self.feedback()["heading_error_rad"]
            ),
        }
