class ControllerError(RuntimeError):
  """A controller failure whose message is safe to show in the UI."""


class BaseController:
  def prepare_step(self, step_index, previous_steering, active):
    """Optional simulator context; inactive steps use recorded steering."""

  def update(self, target_lataccel, current_lataccel, state, future_plan):
    """
    Args:
      target_lataccel: The target lateral acceleration.
      current_lataccel: The current lateral acceleration.
      state: The current state of the vehicle.
      future_plan: The future plan for the next N frames.
    Returns:
      The control signal to be applied to the vehicle.
    """
    raise NotImplementedError
