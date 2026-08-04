from enum import Enum
import numpy as np

OBSTICAL_WRONG_SIDE_LENGTH = 20

class Enviroment:
    def __init__(self, upstream_direction: np.ndarray):
        self.upstream_direction = upstream_direction


class ObstacleType(Enum):
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