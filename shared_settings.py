from enum import Enum
import numpy as np

OBSTICAL_WRONG_SIDE_LENGTH = 20

class Enviroment:
    def __init__(self, upstream_direction: np.ndarray):
        self.upstream_direction = upstream_direction


class ObstacleType(Enum):
    """Mirror of ligmax-pi/nodes/self_driving/obsticales.py.

    **The numbers are a wire format.** The vessel sends the integer and this
    dashboard switches on it, so the original members keep their values for
    ever: append, never renumber, never reuse.
    """

    UNKNOWN = 0

    RED = 1
    GREEN = 2
    NORTH = 3
    SOUTH = 4
    WEST = 5
    EAST = 6

    BOAT = 7
    LAND = 8
    DOCKING_CENTER = 9

    # A mark that reads black-and-yellow before the camera has committed to
    # which of the four cardinals it is. The classifier needs several agreeing
    # votes (`perception/classify.py`), so this is the *ordinary* state of a
    # cardinal for the first seconds it is in view - not an error case, and it
    # has to be drawable, because "cardinal, side unknown" is a far more useful
    # thing to put in front of an operator than "unknown object".
    CARDINAL = 10


class Boat():
    def __init__(self, original_gps_position: np.ndarray, velocity: np.ndarray, heading: np.ndarray):
        self.position = np.array([0,0])
        self.velocity = velocity
        self.heading = heading

        self.original_gps_position = original_gps_position

    def update(self, position: np.ndarray, velocity: np.ndarray, heading: np.ndarray):
        self.position = position
        self.velocity = velocity
        self.heading = heading