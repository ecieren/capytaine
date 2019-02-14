#!/usr/bin/env python
# coding: utf-8
"""Floating bodies to be used in radiation-diffraction problems."""
# This file is part of "Capytaine" (https://github.com/mancellin/capytaine).
# It has been written by Matthieu Ancellin and is released under the terms of the GPLv3 license.

import logging
import copy
from itertools import chain, accumulate, product

import numpy as np

from capytaine.mesh.mesh import Mesh
from capytaine.mesh.meshes_collection import CollectionOfMeshes
from capytaine.tools.geometry import Abstract3DObject, Plane, inplace_transformation

LOG = logging.getLogger(__name__)

TRANSLATION_DOFS_DIRECTIONS = {"surge": (1, 0, 0), "sway": (0, 1, 0), "heave": (0, 0, 1)}
ROTATION_DOFS_AXIS = {"roll": (1, 0, 0), "pitch": (0, 1, 0), "yaw": (0, 0, 1)}


class FloatingBody(Abstract3DObject):

    def __init__(self, mesh=None, dofs=None, name=None):
        """A floating body described as a mesh and some degrees of freedom.

        The mesh structure is stored as a Mesh from capytaine.mesh.mesh or a
        CollectionOfMeshes from capytaine.mesh.meshes_collection.

        The degrees of freedom (dofs) are stored as a dict associating a name to
        a complex-valued array of shape (nb_faces, 3). To each face of the body
        (as indexed in the mesh) corresponds a complex-valued 3d vector, which
        defines the displacement of the center of the face in frequency domain.

        Parameters
        ----------
        mesh : Mesh or CollectionOfMeshes, optional
            the mesh describing the geometry of the floating body.
            If none is given, a empty one is created.
        dofs : dict, optional
            the degrees of freedom of the body.
            If none is given, a empty dictionary is initialized.
        name : str, optional
            a name for the body.
            If none is given, the one of the mesh is used.
        """
        if mesh is None:
            mesh = Mesh(name="dummy_mesh")

        if dofs is None:
            dofs = {}

        if name is None:
            name = mesh.name

        assert isinstance(mesh, Mesh) or isinstance(mesh, CollectionOfMeshes)
        self.mesh = mesh
        self.full_body = None
        self.dofs = dofs
        self.name = name

        LOG.info(f"New floating body: {self.name}.")

    @staticmethod
    def from_file(filename: str, file_format=None, name=None) -> 'FloatingBody':
        """Create a FloatingBody from a mesh file using meshmagick."""
        from capytaine.io.mesh_loaders import load_mesh
        if name is None:
            name = filename
        mesh = load_mesh(filename, file_format, name=f"{name}_mesh")
        return FloatingBody(mesh, name=name)

    def __lt__(self, other: 'FloatingBody') -> bool:
        """Arbitrary order. The point is to sort together the problems involving the same body."""
        return self.name < other.name

    # @property
    # def center_of_buoyancy(self):
    #     return Hydrostatics(self.mesh.merged()).buoyancy_center

    # @property
    # def displacement_volume(self):
    #     return Hydrostatics(self.mesh.merged()).displacement_volume

    # @property
    # def center_of_gravity(self):
    #     if hasattr(self, 'center'):
    #         return self.center

    ##########
    #  Dofs  #
    ##########

    @property
    def nb_dofs(self) -> int:
        """Number of degrees of freedom."""
        return len(self.dofs)

    def add_translation_dof(self, direction=None, name=None, amplitude=1.0) -> None:
        """Add a new translation dof (in place).
        If no direction is given, the code tries to infer it from the name.

        Parameters
        ----------
        direction : array of shape (3,), optional
            the direction of the translation
        name : str, optional
            a name for the degree of freedom
        amplitude : float, optional
            amplitude of the dof (default: 1.0 m/s)
        """
        if direction is None:
            if name is not None and name.lower() in TRANSLATION_DOFS_DIRECTIONS:
                direction = TRANSLATION_DOFS_DIRECTIONS[name.lower()]
            else:
                raise ValueError("A direction needs to be specified for the dof.")

        if name is None:
            name = f"dof_{self.nb_dofs}_translation"

        direction = np.asarray(direction)
        assert direction.shape == (3,)

        motion = np.empty((self.mesh.nb_faces, 3))
        motion[:, :] = direction
        self.dofs[name] = amplitude * motion

    def add_rotation_dof(self, axis=None, name=None, amplitude=1.0) -> None:
        """Add a new rotation dof (in place).
        If no axis is given, the code tries to infer it from the name.

        Parameters
        ----------
        axis: Axis, optional
            the axis of the rotation
        name : str, optional
            a name for the degree of freedom
        amplitude : float, optional
            amplitude of the dof (default: 1.0)
        """
        if axis is None:
            if name is not None and name.lower() in ROTATION_DOFS_AXIS:
                axis_direction = ROTATION_DOFS_AXIS[name.lower()]
                if hasattr(self, 'center'):
                    axis_point = self.center
                    LOG.info(f"The rotation dof {name} has been initialized "
                             f"around the center of gravity of {self.name}.")
                else:
                    axis_point = np.array([0, 0, 0])
                    LOG.warning(f"The rotation dof {name} has been initialized "
                                f"around the origin of the domain (0, 0, 0).")
            else:
                raise ValueError("A direction needs to be specified for the dof.")
        else:
            axis_point = axis.point
            axis_direction = axis.vector

        if name is None:
            name = f"dof_{self.nb_dofs}_rotation"

        if self.mesh.nb_faces == 0:
            self.dofs[name] = np.empty((self.mesh.nb_faces, 3))
        else:
            motion = np.cross(axis_point - self.mesh.faces_centers, axis_direction)
            self.dofs[name] = amplitude * motion

    def add_all_rigid_body_dofs(self) -> None:
        """Add the six degrees of freedom of rigid bodies (in place)."""
        self.add_translation_dof(name="Surge")
        self.add_translation_dof(name="Sway")
        self.add_translation_dof(name="Heave")
        self.add_rotation_dof(name="Roll")
        self.add_rotation_dof(name="Pitch")
        self.add_rotation_dof(name="Yaw")

    ###################
    # Transformations #
    ###################

    def __add__(self, body_to_add: 'FloatingBody') -> 'FloatingBody':
        return self.join_bodies(body_to_add)

    def join_bodies(*bodies, name=None) -> 'FloatingBody':
        if name is None:
            name = "+".join(body.name for body in bodies)
        meshes = CollectionOfMeshes([body.mesh for body in bodies], name=f"{name}_mesh")
        dofs = FloatingBody.combine_dofs(bodies)
        return FloatingBody(mesh=meshes, dofs=dofs, name=name)

    @staticmethod
    def combine_dofs(bodies) -> dict:
        """Combine the degrees of freedom of several bodies."""
        dofs = {}
        cum_nb_faces = accumulate(chain([0], (body.mesh.nb_faces for body in bodies)))
        total_nb_faces = sum(body.mesh.nb_faces for body in bodies)
        for body, nbf in zip(bodies, cum_nb_faces):
            # nbf is the cumulative number of faces of the previous subbodies,
            # that is the offset of the indices of the faces of the current body.
            for name, dof in body.dofs.items():
                new_dof = np.zeros((total_nb_faces, 3))
                new_dof[nbf:nbf+len(dof), :] = dof
                if '__' not in name:
                    new_dof_name = '__'.join([body.name, name])
                else:
                    # The body is probably a combination of bodies already.
                    # So for the associativity of the + operation,
                    # it is better to keep the same name.
                    new_dof_name = name
                dofs[new_dof_name] = new_dof
        return dofs

    def copy(self, name=None) -> 'FloatingBody':
        """Return a deep copy of the body.

        Parameters
        ----------
        name : str, optional
            a name for the new copy
        """
        new_body = copy.deepcopy(self)
        if name is None:
            new_body.name = f"copy_of_{self.name}"
            LOG.debug(f"Copy {self.name}.")
        else:
            new_body.name = name
            LOG.debug(f"Copy {self.name} under the name {name}.")
        return new_body

    def assemble_regular_array(self, distance, nb_bodies):
        """Create an regular array of identical bodies.

        Parameters
        ----------
        distance : float
            Center-to-center distance between objects in the array
        nb_bodies : couple of ints
            Number of objects in the x and y directions.

        Returns
        -------
        FloatingBody
        """
        from capytaine.mesh.symmetries import build_regular_array_of_meshes
        array_mesh = build_regular_array_of_meshes(self.mesh, distance, nb_bodies)
        total_nb_faces = array_mesh.nb_faces
        array_dofs = {}
        for dof_name, dof in self.dofs.items():
            for i, j in product(range(nb_bodies[0]), range(nb_bodies[1])):
                shift_nb_faces = (j*nb_bodies[0] + i) * self.mesh.nb_faces
                new_dof = np.zeros((total_nb_faces, 3))
                new_dof[shift_nb_faces:shift_nb_faces+len(dof), :] = dof
                array_dofs[f'{i}_{j}__{dof_name}'] = new_dof
        return FloatingBody(mesh=array_mesh, dofs=array_dofs, name=f"array_of_{self.name}")

    def extract_faces(self, id_faces_to_extract, return_index=False):
        """Create a new FloatingBody by extracting some faces from the mesh.
        The dofs evolve accordingly.
        """
        if isinstance(self.mesh, CollectionOfMeshes):
            raise NotImplementedError  # TODO

        if return_index:
            new_mesh, id_v = Mesh.extract_faces(self.mesh, id_faces_to_extract, return_index)
        else:
            new_mesh = Mesh.extract_faces(self.mesh, id_faces_to_extract, return_index)
        new_body = FloatingBody(new_mesh)
        LOG.info(f"Extract floating body from {self.name}.")

        new_body.dofs = {}
        for name, dof in self.dofs.items():
            new_body.dofs[name] = dof[id_faces_to_extract, :]

        if return_index:
            return new_body, id_v
        else:
            return new_body

    def splitted_by_plane(self, plane):
        return FloatingBody(mesh=self.mesh.splitted_by_plane(plane), dofs=self.dofs, name=self.name)

    @inplace_transformation
    def mirror(self, plane):
        self.mesh.mirror(plane)
        for dof in self.dofs:
            self.dofs[dof] -= 2 * np.outer(np.dot(self.dofs[dof], plane.normal), plane.normal)
        if hasattr(self, 'center'):
            print(np.dot(self.center, plane.normal) - plane.c, plane.normal)
            self.center -= 2 * (np.dot(self.center, plane.normal) - plane.c) * plane.normal
        return self

    @inplace_transformation
    def translate(self, *args):
        self.mesh.translate(*args)
        if hasattr(self, 'center'):
            self.center += args[0]
        return self

    @inplace_transformation
    def rotate(self, axis, angle):
        matrix = axis.rotation_matrix(angle)
        self.mesh.rotate(axis, angle)
        if hasattr(self, 'center'):
            self.center = matrix @ self.center
        for dof in self.dofs:
            self.dofs[dof] = (matrix @ self.dofs[dof].T).T
        return self

    @inplace_transformation
    def clip(self, plane):
        # Keep of copy of the full mesh
        if self.full_body is None:
            self.full_body = self.copy()

        # Clip mesh
        LOG.info(f"Clipping {self.name} with respect to {plane}")
        self.mesh.clip(plane)

        # Clip dofs
        ids = self.mesh._clipping_data['faces_ids']
        for dof in self.dofs:
            if len(ids) > 0:
                self.dofs[dof] = self.dofs[dof][ids]
            else:
                self.dofs[dof] = np.empty((0, 3))
        return self

    @inplace_transformation
    def keep_immersed_part(self, free_surface=0.0, sea_bottom=-np.infty):
        """Remove the parts of the mesh above the sea bottom and below the free surface."""
        self.clip(Plane(normal=(0, 0, 1), point=(0, 0, free_surface)))
        if sea_bottom > -np.infty:
            self.clip(Plane(normal=(0, 0, -1), point=(0, 0, sea_bottom)))
        return self

    #############
    #  Display  #
    #############

    def __str__(self):
        return self.name

    def __repr__(self):
        return (f"{self.__class__.__name__}(mesh={self.mesh.name}, "
                f"dofs={{{', '.join(self.dofs.keys())}}}, name={self.name})")

    def show(self, **kwargs):
        from capytaine.ui.vtk.body_viewer import FloatingBodyViewer
        viewer = FloatingBodyViewer()
        viewer.add_body(self, **kwargs)
        viewer.show()
        viewer.finalize()

    def show_matplotlib(self, *args, **kwargs):
        return self.mesh.show_matplotlib(*args, **kwargs)

