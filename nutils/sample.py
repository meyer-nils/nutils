'''
The sample module defines the :class:`Sample` class, which represents a
collection of discrete points on a topology and is typically formed via
:func:`nutils.topology.Topology.sample`. Any function evaluation starts from
this sampling step, which drops element information and other topological
properties such as boundaries and groups, but retains point positions and
(optionally) integration weights. Evaluation is performed by subsequent calls
to :func:`Sample.integrate`, :func:`Sample.integral` or :func:`Sample.eval`.

Besides the location of points, :class:`Sample` also keeps track of point
connectivity through its :attr:`Sample.tri` and :attr:`Sample.hull`
properties, representing a (n-dimensional) triangulation of the interior and
boundary, respectively. Availability of these properties depends on the
selected sample points, and is typically used in combination with the "bezier"
set.
'''

from . import types, points, _util as util, function, evaluable, parallel, numeric, matrix, sparse, warnings
from .pointsseq import PointsSequence
from .transformseq import Transforms
from ._backports import cached_property
from typing import Iterable, Mapping, Optional, Sequence, Tuple, Union
import numpy
import numbers
import collections.abc
import os
import treelog as log
import abc

_PointsShape = Tuple[evaluable.Array, ...]
_TransformChainsMap = Mapping[str, Tuple[Tuple[Transforms, ...], int]]
_CoordinatesMap = Mapping[str, evaluable.Array]


def argdict(arguments) -> Mapping[str, numpy.ndarray]:
    if len(arguments) == 1 and 'arguments' in arguments and isinstance(arguments['arguments'], collections.abc.Mapping):
        return arguments['arguments']
    return arguments


class Sample(types.Singleton):
    '''Collection of points on a topology.

    The :class:`Sample` class represents a collection of discrete points on a
    topology and is typically formed via :func:`nutils.topology.Topology.sample`.
    Any function evaluation starts from this sampling step, which drops element
    information and other topological properties such as boundaries and groups,
    but retains point positions and (optionally) integration weights. Evaluation
    is performed by subsequent calls to :func:`integrate`, :func:`integral` or
    :func:`eval`.

    Besides the location of points, ``Sample`` also keeps track of point
    connectivity through its :attr:`tri` and :attr:`hull` properties,
    representing a (n-dimensional) triangulation of the interior and boundary,
    respectively. Availability of these properties depends on the selected sample
    points, and is typically used in combination with the "bezier" set.
    '''

    @staticmethod
    def new(space: str, transforms: Iterable[Transforms], points: PointsSequence, index: Optional[Union[numpy.ndarray, Sequence[numpy.ndarray]]] = None) -> 'Sample':
        '''Create a new :class:`Sample`.

        Parameters
        ----------
        transforms : :class:`tuple` or transformation chains
            List of transformation chains leading to local coordinate systems that
            contain points.
        points : :class:`~nutils.pointsseq.PointsSequence`
            Points sequence.
        index : integer array or :class:`tuple` of integer arrays, optional
            Indices defining the order in which points show up in the evaluation.
            If absent the indices will be strictly increasing.
        '''

        sample = _DefaultIndex(space, tuple(transforms), points)
        if index is not None:
            if isinstance(index, (tuple, list)):
                assert all(ind.shape == (pnt.npoints,) for ind, pnt in zip(index, points))
                index = numpy.concatenate(index)
            sample = _CustomIndex(sample, types.arraydata(index))
        return sample

    @staticmethod
    def empty(spaces: Tuple[str, ...], ndims: int) -> 'Sample':
        return _Empty(spaces, ndims)

    def __init__(self, spaces: Tuple[str, ...], ndims: int, nelems: int, npoints: int) -> None:
        '''
        parameters
        ----------
        spaces : :class:`tuple` of :class:`str`
            The names of the spaces on which this sample is defined.
        ndims : :class:`int`
            The dimension of the coordinates.
        nelems : :class:`int`
            The number of elements.
        npoints : :class:`int`
            The number of points.
        '''

        self.spaces = spaces
        self.ndims = ndims
        self.nelems = nelems
        self.npoints = npoints

    def __add__(self, other: 'Sample') -> 'Sample':
        if not isinstance(other, Sample):
            return NotImplemented
        elif self.spaces != other.spaces:
            raise ValueError('Cannot add samples with different spaces.')
        elif other.npoints == 0:
            return self
        elif self.npoints == 0:
            return other
        else:
            return _Add(self, other)

    def __mul__(self, other: 'Sample') -> 'Sample':
        if not isinstance(other, Sample):
            return NotImplemented
        elif not set(self.spaces).isdisjoint(set(other.spaces)):
            raise ValueError('Cannot multiply samples with common spaces.')
        else:
            return _Mul(self, other)

    def __repr__(self) -> str:
        return '{}.{}<{}D, {} elems, {} points>'.format(type(self).__module__, type(self).__qualname__, self.ndims, self.nelems, self.npoints)

    @property
    def index(self) -> Tuple[numpy.ndarray, ...]:
        return tuple(map(self.getindex, range(self.nelems)))

    def getindex(self, __ielem: int) -> numpy.ndarray:
        '''Return the indices of `Sample.points[ielem]` in results of `Sample.eval`.'''

        raise NotImplementedError

    def get_evaluable_indices(self, __ielem: evaluable.Array) -> evaluable.Array:
        '''Return the evaluable indices for the given evaluable element index.

        Parameters
        ----------
        ielem : :class:`nutils.evaluable.Array`, ndim: 0, dtype: :class:`int`
            The element index.

        Returns
        -------
        indices : :class:`nutils.evaluable.Array`
            The indices of the points belonging to the given element as a 1D array.

        See Also
        --------
        :meth:`getindex` : the non-evaluable equivalent
        '''

        raise NotImplementedError

    def get_evaluable_weights(self, __ielem: evaluable.Array) -> evaluable.Array:
        raise NotImplementedError

    def get_lower_args(self, __ielem: evaluable.Array) -> function.LowerArgs:
        raise NotImplementedError

    @util.positional_only
    @util.single_or_multiple
    def integrate(self, funcs: Iterable[function.IntoArray], arguments: Mapping[str, numpy.ndarray] = ...) -> Tuple[numpy.ndarray, ...]:
        '''Integrate functions.

        Args
        ----
        funcs : :class:`nutils.function.Array` object or :class:`tuple` thereof.
            The integrand(s).
        arguments : :class:`dict` (default: None)
            Optional arguments for function evaluation.
        '''

        funcs, funcscales = zip(*map(function.Array.cast_withscale, funcs))
        datas = self.integrate_sparse(funcs, argdict(arguments))
        with log.iter.fraction('assembling', datas) as items:
            return tuple(_convert(data, inplace=True) * scale for data, scale in zip(items, funcscales))

    @util.single_or_multiple
    def integrate_sparse(self, funcs: Iterable[function.IntoArray], arguments: Optional[Mapping[str, numpy.ndarray]] = None) -> Tuple[numpy.ndarray, ...]:
        '''Integrate functions into sparse data.

        Args
        ----
        funcs : :class:`nutils.function.Array` object or :class:`tuple` thereof.
            The integrand(s).
        arguments : :class:`dict` (default: None)
            Optional arguments for function evaluation.
        '''

        return evaluable.eval_sparse(map(self.integral, funcs), **(arguments or {}))

    def integral(self, __func: function.IntoArray) -> function.Array:
        '''Create Integral object for postponed integration.

        Args
        ----
        func : :class:`nutils.function.Array`
            Integrand.
        '''

        func, funcscale = function.Array.cast_withscale(__func)
        return _Integral(func, self) * funcscale

    @util.positional_only
    @util.single_or_multiple
    def eval(self, funcs: Iterable[function.IntoArray], arguments: Mapping[str, numpy.ndarray] = ...) -> Tuple[numpy.ndarray, ...]:
        '''Evaluate function.

        Args
        ----
        funcs : :class:`nutils.function.Array` object or :class:`tuple` thereof.
            The integrand(s).
        arguments : :class:`dict` (default: None)
            Optional arguments for function evaluation.
        '''

        funcs, funcscales = zip(*map(function.Array.cast_withscale, funcs))
        datas = self.eval_sparse(funcs, arguments)
        with log.iter.fraction('assembling', datas) as items:
            return tuple(sparse.toarray(data) * funcscale for data, funcscale in zip(datas, funcscales))

    @util.positional_only
    @util.single_or_multiple
    def eval_sparse(self, funcs: Iterable[function.IntoArray], arguments: Optional[Mapping[str, numpy.ndarray]] = None) -> Tuple[numpy.ndarray, ...]:
        '''Evaluate function.

        Args
        ----
        funcs : :class:`nutils.function.Array` object or :class:`tuple` thereof.
            The integrand(s).
        arguments : :class:`dict` (default: None)
            Optional arguments for function evaluation.
        '''

        return evaluable.eval_sparse(map(self, funcs), **(arguments or {}))

    def __call__(self, __func: function.IntoArray) -> function.Array:
        func, funcscale = function.Array.cast_withscale(__func)
        ielem = evaluable.loop_index('_sample_' + '_'.join(self.spaces), self.nelems)
        indices = evaluable.loop_concatenate(evaluable._flat(self.get_evaluable_indices(ielem)), ielem)
        return _ReorderPoints(_ConcatenatePoints(func, self), indices) * funcscale

    def basis(self, interpolation: str = 'none') -> function.Array:
        '''Basis-like function that for every point in the sample evaluates to the
        unit vector corresponding to its index.

        Args
        ----
        interpolation : :class:`str`
            Same as in :meth:`asfunction`.
        '''

        raise NotImplementedError

    def asfunction(self, array: numpy.ndarray, interpolation: str = 'none') -> function.Array:
        '''Convert sampled data to evaluable array.

        Using the result of :func:`Sample.eval`, create a sampled array that upon
        evaluation recovers the original function in the set of points matching the
        original sampling.

        >>> from nutils import mesh
        >>> domain, geom = mesh.rectilinear([1,2])
        >>> gauss = domain.sample('gauss', 2)
        >>> data = gauss.eval(geom)
        >>> sampled = gauss.asfunction(data)
        >>> domain.integrate(sampled, degree=2)
        array([ 1.,  2.])

        Args
        ----
        array :
            The sampled data.
        interpolation : :class:`str`
            Interpolation scheme used to map sample values to the evaluating
            sample. Valid values are "none", demanding that the evaluating
            sample mathes self, or "nearest" for nearest-neighbour mapping.
        '''

        return function.matmat(self.basis(interpolation=interpolation), array)

    @property
    def tri(self) -> numpy.ndarray:
        '''Triangulation of interior.

        A two-dimensional integer array with ``ndims+1`` columns, of which every
        row defines a simplex by mapping vertices into the list of points.
        '''

        return numpy.concatenate([numpy.take(self.getindex(i), self.get_element_tri(i)) for i in range(self.nelems)], axis=0) if self.nelems else numpy.zeros((0, self.ndims+1), int)

    def get_element_tri(self, __ielem: int) -> numpy.ndarray:
        raise NotImplementedError

    @property
    def hull(self) -> numpy.ndarray:
        '''Triangulation of the exterior hull.

        A two-dimensional integer array with ``ndims`` columns, of which every row
        defines a simplex by mapping vertices into the list of points. Note that
        the hull often does contain internal element boundaries as the
        triangulations originating from separate elements are disconnected.
        '''

        return numpy.concatenate([numpy.take(self.getindex(i), self.get_element_hull(i)) for i in range(self.nelems)], axis=0) if self.nelems else numpy.zeros((0, self.ndims), int)

    def get_element_hull(self, __ielem: int) -> numpy.ndarray:
        raise NotImplementedError

    def subset(self, __mask: numpy.ndarray) -> 'Sample':
        '''Reduce the number of points.

        Simple selection mechanism that returns a reduced Sample based on a
        selection mask. Points that are marked True will still be part of the new
        subset; points marked False may be dropped but this is not guaranteed. The
        point order of the original Sample is preserved.

        Args
        ----
        mask : :class:`bool` array.
            Boolean mask that selects all points that should remain. The resulting
            Sample may contain more points than this, but not less.

        Returns
        -------
        subset : :class:`Sample`
        '''

        return self.take_elements(numpy.array([ielem for ielem in range(self.nelems) if __mask[self.getindex(ielem)].any()]))

    def take_elements(self, __indices: numpy.ndarray) -> 'Sample':
        if len(__indices):
            return _TakeElements(self, types.arraydata(__indices))
        else:
            return Sample.empty(self.spaces, self.ndims)

    def zip(*samples: 'Sample') -> 'Sample':
        '''
        Join multiple samples, with identical point count but differing spaces, into
        a new sample with the same point count and the total of spaces. The result is
        a sample that is able to evaluate any function that is evaluable on at least
        one of the individual samples.

        >>> from . import mesh
        >>> topo1, geom1 = mesh.line([0,.5,1], space='X')
        >>> topo2, geom2 = mesh.line([0,.2,1], space='Y')
        >>> sample1 = topo1.sample('uniform', 3)
        >>> sample2 = topo2.locate(geom2, sample1.eval(geom1), tol=1e-10)
        >>> zipped = sample1.zip(sample2)
        >>> numpy.linalg.norm(zipped.eval(geom1 - geom2))
        0.±1e-10

        Though zipping is almost entirely symmetric, the first argument has special
        status in case the zipped sample is used to form an integral, in which
        case the first sample provides the quadrature weights. This means that
        integrals involving any but the first sample's geometry scale incorrectly.

        >>> zipped.integrate(function.J(geom1)) # correct
        1.0
        >>> zipped.integrate(function.J(geom2)) # wrong (expected 1)
        1.4±1e-10

        Args
        ----
        samples : :class:`Sample`
            Samples that are to be zipped together.

        Returns
        -------
        zipped : :class:`Sample`
        '''

        return _Zip(*samples)


class _TransformChainsSample(Sample):

    def __init__(self, space: str, transforms: Tuple[Transforms, ...], points: PointsSequence) -> None:
        '''
        parameters
        ----------
        space : ::class:`str`
            The name of the space on which this sample is defined.
        transforms : :class:`tuple` or transformation chains
            List of transformation chains leading to local coordinate systems that
            contain points.
        points : :class:`~nutils.pointsseq.PointsSequence`
            Points sequence.
        '''

        assert len(transforms) >= 1
        assert all(len(t) == len(points) for t in transforms)
        self.space = space
        self.transforms = transforms
        self.points = points
        super().__init__((space,), transforms[0].fromdims, len(points), points.npoints)

    def get_evaluable_weights(self, __ielem: evaluable.Array) -> evaluable.Array:
        return self.points.get_evaluable_weights(__ielem)

    def get_lower_args(self, __ielem: evaluable.Array) -> function.LowerArgs:
        return function.LowerArgs.for_space(self.space, self.transforms, __ielem, self.points.get_evaluable_coords(__ielem))

    def basis(self, interpolation: str = 'none') -> function.Array:
        return _Basis(self, interpolation)

    def subset(self, mask: numpy.ndarray) -> Sample:
        selection = types.frozenarray([ielem for ielem in range(self.nelems) if mask[self.getindex(ielem)].any()])
        transforms = tuple(transform[selection] for transform in self.transforms)
        return Sample.new(self.space, transforms, self.points.take(selection))

    def get_element_tri(self, ielem: int) -> numpy.ndarray:
        if not 0 <= ielem < self.nelems:
            raise IndexError('index ouf of range')
        return self.points.get(ielem).tri

    def get_element_hull(self, ielem: int) -> numpy.ndarray:
        if not 0 <= ielem < self.nelems:
            raise IndexError('index ouf of range')
        return self.points.get(ielem).hull


class _DefaultIndex(_TransformChainsSample):

    @cached_property
    def offsets(self) -> numpy.ndarray:
        return types.frozenarray(numpy.cumsum([0]+[p.npoints for p in self.points]), copy=False)

    def getindex(self, ielem: int) -> numpy.ndarray:
        if not 0 <= ielem < self.nelems:
            raise IndexError('index out of range')
        return types.frozenarray(numpy.arange(*self.offsets[ielem:ielem+2]), copy=False)

    @property
    def tri(self) -> numpy.ndarray:
        return self.points.tri

    @property
    def hull(self) -> numpy.ndarray:
        return self.points.hull

    def get_evaluable_indices(self, ielem: evaluable.Array) -> evaluable.Array:
        npoints = self.points.get_evaluable_coords(ielem).shape[0]
        offset = evaluable.get(_offsets(self.points), 0, ielem)
        return evaluable.Range(npoints) + offset

    def __call__(self, __func: function.IntoArray) -> function.Array:
        func, funcscale = function.Array.cast_withscale(__func)
        return _ConcatenatePoints(func, self) * funcscale


class _CustomIndex(_TransformChainsSample):

    def __init__(self, parent: Sample, index: types.arraydata) -> None:
        assert isinstance(index, types.arraydata)
        assert index.shape == (parent.npoints,)
        self._parent = parent
        self._index = index
        super().__init__(parent.space, parent.transforms, parent.points)

    def getindex(self, ielem: int) -> numpy.ndarray:
        return numpy.take(self._index, self._parent.getindex(ielem))

    def get_evaluable_indices(self, ielem: evaluable.Array) -> evaluable.Array:
        return evaluable.Take(evaluable.Constant(self._index), self._parent.get_evaluable_indices(ielem))

    @property
    def tri(self) -> numpy.ndarray:
        return numpy.take(self._index, self._parent.tri)

    @property
    def hull(self) -> numpy.ndarray:
        return numpy.take(self._index, self._parent.hull)


if os.environ.get('NUTILS_TENSORIAL', None) == 'test':  # pragma: nocover

    from unittest import SkipTest

    class _TensorialSample(Sample):

        def getindex(self, ielem: int) -> numpy.ndarray:
            raise SkipTest('`{}` does not implement `Sample.getindex`'.format(type(self).__qualname__))

        def get_evaluable_indices(self, __ielem: evaluable.Array) -> evaluable.Array:
            raise SkipTest('`{}` does not implement `Sample.get_evaluable_indices`'.format(type(self).__qualname__))

        def get_evaluable_weights(self, __ielem: evaluable.Array) -> evaluable.Array:
            raise SkipTest('`{}` does not implement `Sample.get_evaluable_weights`'.format(type(self).__qualname__))

        def get_lower_args(self, __ielem: evaluable.Array) -> function.LowerArgs:
            raise SkipTest('`{}` does not implement `Sample.get_lower_args`'.format(type(self).__qualname__))

        @property
        def transforms(self) -> Tuple[Transforms, ...]:
            raise SkipTest('`{}` does not implement `Sample.transforms`'.format(type(self).__qualname__))

        @property
        def points(self) -> Tuple[Transforms, ...]:
            raise SkipTest('`{}` does not implement `Sample.points`'.format(type(self).__qualname__))

        def basis(self, interpolation: str = 'none') -> function.Array:
            raise SkipTest('`{}` does not implement `Sample.basis`'.format(type(self).__qualname__))

else:
    _TensorialSample = Sample


class _Empty(_TensorialSample):

    def __init__(self, spaces: Tuple[str, ...], ndims: int) -> None:
        super().__init__(spaces, ndims, 0, 0)

    def getindex(self, __ielem: int) -> numpy.ndarray:
        raise IndexError('index out of range')

    def get_evaluable_indices(self, __ielem: evaluable.Array) -> evaluable.Array:
        return evaluable.Zeros((evaluable.constant(0),) * len(self.spaces), dtype=int)

    def get_evaluable_weights(self, __ielem: evaluable.Array) -> evaluable.Array:
        return evaluable.Zeros((evaluable.constant(0),) * len(self.spaces), dtype=float)

    def get_lower_args(self, __ielem: evaluable.Array) -> function.LowerArgs:
        return function.LowerArgs((), {}, {})

    def get_element_tri(self, ielem: int) -> numpy.ndarray:
        raise IndexError('index out of range')

    def get_element_hull(self, ielem: int) -> numpy.ndarray:
        raise IndexError('index out of range')

    def take_elements(self, __indices: numpy.ndarray) -> Sample:
        return self

    def integral(self, __func: function.IntoArray) -> function.Array:
        func = function.Array.cast(__func)
        return function.zeros(func.shape, func.dtype)

    def __call__(self, __func: function.IntoArray) -> function.Array:
        func, funcscale = function.Array.cast_withscale(__func)
        return function.zeros((0, *func.shape), func.dtype) * funcscale

    def basis(self, interpolation: str = 'none') -> function.Array:
        return function.zeros((0,), float)


class _Add(_TensorialSample):

    def __init__(self, sample1: Sample, sample2: Sample) -> None:
        assert sample1.spaces == sample2.spaces
        self._sample1 = sample1
        self._sample2 = sample2
        super().__init__(sample1.spaces, sample1.ndims, sample1.nelems + sample2.nelems, sample1.npoints + sample2.npoints)

    def getindex(self, ielem: int) -> numpy.ndarray:
        if ielem < self._sample1.nelems:
            return self._sample1.getindex(ielem)
        else:
            return self._sample2.getindex(ielem - self._sample1.nelems) + self._sample1.npoints

    def get_element_tri(self, ielem: int) -> numpy.ndarray:
        if ielem < self._sample1.nelems:
            return self._sample1.get_element_tri(ielem)
        else:
            return self._sample2.get_element_tri(ielem - self._sample1.nelems)

    def get_element_hull(self, ielem: int) -> numpy.ndarray:
        if ielem < self._sample1.nelems:
            return self._sample1.get_element_hull(ielem)
        else:
            return self._sample2.get_element_hull(ielem - self._sample1.nelems)

    @property
    def tri(self) -> numpy.ndarray:
        return numpy.concatenate([self._sample1.tri, self._sample2.tri + self._sample1.npoints])

    @property
    def hull(self) -> numpy.ndarray:
        return numpy.concatenate([self._sample1.hull, self._sample2.hull + self._sample1.npoints])

    def take_elements(self, __indices: numpy.ndarray) -> Sample:
        mask = numpy.less(__indices, self._sample1.nelems)
        sample1 = self._sample1.take_elements(__indices[mask])
        sample2 = self._sample2.take_elements(__indices[~mask] - self._sample1.nelems)
        return sample1 + sample2

    def integral(self, func: function.IntoArray) -> function.Array:
        return self._sample1.integral(func) + self._sample2.integral(func)

    def __call__(self, func: function.IntoArray) -> function.Array:
        return numpy.concatenate([self._sample1(func), self._sample2(func)])


class _Mul(_TensorialSample):

    def __init__(self, sample1: Sample, sample2: Sample) -> None:
        assert set(sample1.spaces).isdisjoint(set(sample2.spaces))
        self._sample1 = sample1
        self._sample2 = sample2
        super().__init__(sample1.spaces + sample2.spaces, sample1.ndims + sample2.ndims, sample1.nelems * sample2.nelems, sample1.npoints * sample2.npoints)

    def getindex(self, __ielem: int) -> numpy.ndarray:
        ielem1, ielem2 = divmod(__ielem, self._sample2.nelems)
        index1 = self._sample1.getindex(ielem1)
        index2 = self._sample2.getindex(ielem2)
        return (index1[:, None] * self._sample2.npoints + index2[None, :]).ravel()

    def get_evaluable_indices(self, __ielem: evaluable.Array) -> evaluable.Array:
        ielem1, ielem2 = evaluable.divmod(__ielem, self._sample2.nelems)
        index1 = self._sample1.get_evaluable_indices(ielem1)
        index2 = self._sample2.get_evaluable_indices(ielem2)
        return evaluable.appendaxes(index1 * self._sample2.npoints, index2.shape) + evaluable.prependaxes(index2, index1.shape)

    def get_evaluable_weights(self, __ielem: evaluable.Array) -> evaluable.Array:
        ielem1, ielem2 = evaluable.divmod(__ielem, self._sample2.nelems)
        weights1 = self._sample1.get_evaluable_weights(ielem1)
        weights2 = self._sample2.get_evaluable_weights(ielem2)
        return evaluable.einsum('A,B->AB', weights1, weights2)

    def get_lower_args(self, __ielem: evaluable.Array) -> function.LowerArgs:
        ielem1, ielem2 = evaluable.divmod(__ielem, self._sample2.nelems)
        return self._sample1.get_lower_args(ielem1) | self._sample2.get_lower_args(ielem2)

    def get_element_tri(self, ielem: int) -> numpy.ndarray:
        if self._sample1.ndims == 1:
            ielem1, ielem2 = divmod(ielem, self._sample2.nelems)
            npoints2 = len(self._sample2.getindex(ielem2))
            tri12 = self._sample1.get_element_tri(ielem1)[:, None, :, None] * npoints2 + self._sample2.get_element_tri(ielem2)[None, :, None, :]  # ntri1 x ntri2 x 2 x ndims
            return numeric.overlapping(tri12.reshape(-1, 2*self.ndims), n=self.ndims+1).reshape(-1, self.ndims+1)
        elif self._sample1.npoints == 1:
            return self._sample2.get_element_tri(ielem)
        elif self._sample2.npoints == 1:
            return self._sample1.get_element_tri(ielem)
        else:
            return super().get_element_tri(ielem)

    def get_element_hull(self, ielem: int) -> numpy.ndarray:
        if self._sample1.ndims == 1:
            ielem1, ielem2 = divmod(ielem, self._sample2.nelems)
            npoints2 = len(self._sample2.getindex(ielem2))
            hull1 = self._sample1.get_element_hull(ielem1)[:, None, :, None] * npoints2 + self._sample2.get_element_tri(ielem2)[None, :, None, :]  # 2 x ntri2 x 1 x ndims
            hull2 = self._sample1.get_element_tri(ielem1)[:, None, :, None] * npoints2 + self._sample2.get_element_hull(ielem2)[None, :, None, :]  # ntri1 x nhull2 x 2 x ndims-1
            return numpy.concatenate([hull1.reshape(-1, self.ndims), numeric.overlapping(hull2.reshape(-1, 2*(self.ndims-1)), n=self.ndims).reshape(-1, self.ndims)])
        elif self._sample1.npoints == 1:
            return self._sample2.get_element_hull(ielem)
        elif self._sample2.npoints == 1:
            return self._sample1.get_element_hull(ielem)
        else:
            return super().get_element_hull(ielem)

    @property
    def tri(self) -> numpy.ndarray:
        if self._sample1.ndims == 1:
            tri12 = self._sample1.tri[:, None, :, None] * self._sample2.npoints + self._sample2.tri[None, :, None, :]  # ntri1 x ntri2 x 2 x ndims
            return numeric.overlapping(tri12.reshape(-1, 2*self.ndims), n=self.ndims+1).reshape(-1, self.ndims+1)
        else:
            return super().tri

    @property
    def hull(self) -> numpy.ndarray:
        if self._sample1.ndims == 1:
            hull1 = self._sample1.hull[:, None, :, None] * self._sample2.npoints + self._sample2.tri[None, :, None, :]  # 2 x ntri2 x 1 x ndims
            hull2 = self._sample1.tri[:, None, :, None] * self._sample2.npoints + self._sample2.hull[None, :, None, :]  # ntri1 x nhull2 x 2 x ndims-1
            return numpy.concatenate([hull1.reshape(-1, self.ndims), numeric.overlapping(hull2.reshape(-1, 2*(self.ndims-1)), n=self.ndims).reshape(-1, self.ndims)])
        else:
            return super().hull

    def integral(self, func: function.IntoArray) -> function.Array:
        return self._sample1.integral(self._sample2.integral(func))

    def __call__(self, func: function.IntoArray) -> function.Array:
        return numpy.reshape(self._sample1(self._sample2(func)), (-1, *func.shape))

    def basis(self, interpolation: str = 'none') -> Sample:
        basis1 = self._sample1.basis(interpolation)
        basis2 = self._sample2.basis(interpolation)
        assert basis1.ndim == basis2.ndim == 1
        return numpy.ravel(basis1[:, None] * basis2[None, :])


class _Zip(Sample):

    def __init__(self, *samples):
        npoints = samples[0].npoints
        spaces = util.sum(sample.spaces for sample in samples)
        if not all(sample.npoints == npoints for sample in samples):
            raise ValueError('points do not match')
        if len(set(spaces)) < len(spaces):
            raise ValueError('spaces overlap')

        self._samples = samples

        ielems = numpy.empty((len(samples), npoints), dtype=int)
        ilocals = numpy.empty((len(samples), npoints), dtype=int)
        for isample, sample in enumerate(samples):
            for ielem in range(sample.nelems):
                indices = sample.getindex(ielem)
                ielems[isample, indices] = ielem
                ilocals[isample, indices] = numpy.arange(indices.shape[0])

        nelems = tuple(sample.nelems for sample in samples)
        flat_ielems = numpy.ravel_multi_index(ielems, nelems)
        flat_ielems, inverse, sizes = numpy.unique(flat_ielems, return_inverse=True, return_counts=True)
        self._offsets = types.arraydata(numpy.cumsum([0, *sizes]))
        self._sizes = types.arraydata(sizes)
        self._indices = types.arraydata(numpy.argsort(inverse))
        self._ielems = tuple(types.arraydata(array) for array in numpy.unravel_index(flat_ielems, nelems))
        self._ilocals = tuple(types.arraydata(numpy.take(ilocal, self._indices, axis=0)) for ilocal in ilocals)

        super().__init__(spaces=spaces, ndims=samples[0].ndims, nelems=self._sizes.shape[0], npoints=npoints)

    def getindex(self, ielem):
        return numpy.asarray(self._indices)[slice(*numpy.asarray(self._offsets)[ielem:ielem+2])]

    def _getslice(self, ielem):
        return evaluable.Take(evaluable.Constant(self._offsets), ielem) + evaluable.Range(evaluable.Take(evaluable.Constant(self._sizes), ielem))

    def get_lower_args(self, __ielem: evaluable.Array) -> function.LowerArgs:
        points_shape = evaluable.Take(evaluable.Constant(self._sizes), __ielem),
        coordinates = {}
        transform_chains = {}
        for samplei, ielemsi, ilocalsi in zip(self._samples, self._ielems, self._ilocals):
            argsi = samplei.get_lower_args(evaluable.Take(evaluable.Constant(ielemsi), __ielem))
            slicei = evaluable.Take(evaluable.Constant(ilocalsi), self._getslice(__ielem))
            transform_chains.update(argsi.transform_chains)
            coordinates.update({space: evaluable._take(coords, slicei, axis=0) for space, coords in argsi.coordinates.items()})
        return function.LowerArgs(points_shape, transform_chains, coordinates)

    def get_evaluable_indices(self, ielem):
        return evaluable.Take(evaluable.Constant(self._indices), self._getslice(ielem))

    def get_evaluable_weights(self, ielem):
        ielem0 = evaluable.Take(evaluable.Constant(self._ielems[0]), ielem)
        slice0 = evaluable.Take(evaluable.Constant(self._ilocals[0]), self._getslice(ielem))
        weights = self._samples[0].get_evaluable_weights(ielem0)
        return evaluable._take(weights, slice0, axis=0)


class _TakeElements(_TensorialSample):

    def __init__(self, parent: Sample, indices: types.arraydata) -> None:
        assert isinstance(indices, types.arraydata) and indices.ndim == 1 and indices.shape[0] > 0, f'indices={indices!r}'
        self._parent = parent
        self._indices = indices
        super().__init__(parent.spaces, parent.ndims, self._indices.shape[0], self._offsets[-1])

    @cached_property
    def _offsets(self) -> numpy.ndarray:
        return types.frozenarray(numpy.cumsum([0]+[len(self._parent.getindex(i)) for i in numpy.asarray(self._indices)]))

    def getindex(self, ielem: int) -> numpy.ndarray:
        if not 0 <= ielem < self.nelems:
            raise IndexError('index out of range')
        return numpy.arange(self._offsets[ielem], self._offsets[ielem+1])

    def get_evaluable_indices(self, __ielem: evaluable.Array) -> evaluable.Array:
        i = evaluable.loop_index('_i', self.nelems)
        iparent = evaluable.Take(evaluable.Constant(self._indices), i)
        sizes = evaluable.loop_concatenate(evaluable.InsertAxis(self._parent.get_evaluable_indices(iparent).size, evaluable.constant(1)), i)
        offsets = evaluable._SizesToOffsets(sizes)

        iparent = evaluable.Take(evaluable.Constant(self._indices), __ielem)
        shape = self._parent.get_evaluable_indices(iparent).shape
        pshape = [shape[-1]]
        for n in reversed(shape[:-1]):
            pshape.insert(0, pshape[0] * n)
        indices = evaluable.Range(pshape[0]) + evaluable.Take(offsets, __ielem)
        for a, b in zip(shape[:-1], pshape[1:]):
            indices = evaluable.Unravel(indices, a, b)
        return indices

    def get_evaluable_weights(self, __ielem: evaluable.Array) -> evaluable.Array:
        return self._parent.get_evaluable_weights(evaluable.Take(evaluable.Constant(self._indices), __ielem))

    def get_lower_args(self, __ielem: evaluable.Array) -> function.LowerArgs:
        return self._parent.get_lower_args(evaluable.Take(evaluable.Constant(self._indices), __ielem))

    def get_element_tri(self, __ielem: int) -> numpy.ndarray:
        if not 0 <= __ielem < self.nelems:
            raise IndexError('index ouf of range')
        return self._parent.get_element_tri(numpy.take(self._indices, __ielem))

    def get_element_hull(self, __ielem: int) -> numpy.ndarray:
        if not 0 <= __ielem < self.nelems:
            raise IndexError('index ouf of range')
        return self._parent.get_element_hull(numpy.take(self._indices, __ielem))

    def take_elements(self, __indices: numpy.ndarray) -> Sample:
        return self._parent.take_elements(numpy.take(self._indices, __indices))


def eval_integrals(*integrals: evaluable.AsEvaluableArray, **arguments: Mapping[str, numpy.ndarray]) -> Tuple[Union[numpy.ndarray, matrix.Matrix], ...]:
    '''Evaluate integrals.

    Evaluate one or several postponed integrals. By evaluating them
    simultaneously, rather than using :meth:`nutils.function.Array.eval` on each
    integral individually, integrations will be grouped per Sample and jointly
    executed, potentially increasing efficiency.

    Args
    ----
    integrals : :class:`tuple` of integrals
        Integrals to be evaluated.
    arguments : :class:`dict` (default: None)
        Optional arguments for function evaluation.

    Returns
    -------
    results : :class:`tuple` of arrays and/or :class:`nutils.matrix.Matrix` objects.
    '''

    with log.iter.fraction('assembling', evaluable.eval_sparse(integrals, **argdict(arguments))) as retvals:
        return tuple(_convert(retval, inplace=True) for retval in retvals)


def eval_integrals_sparse(*integrals: evaluable.AsEvaluableArray, **arguments: Mapping[str, numpy.ndarray]) -> Tuple[numpy.ndarray, ...]:
    '''
    .. deprecated:: 7.0
        sample.eval_integrals_sparse is deprecated, use function.eval_sparse instead

    Evaluate integrals into sparse data.

    Evaluate one or several postponed integrals. By evaluating them
    simultaneously, rather than using :meth:`nutils.function.Array.eval` on each
    integral individually, integrations will be grouped per Sample and jointly
    executed, potentially increasing efficiency.

    Args
    ----
    integrals : :class:`tuple` of integrals
        Integrals to be evaluated.
    arguments : :class:`dict` (default: None)
        Optional arguments for function evaluation.

    Returns
    -------
    results : :class:`tuple` of arrays and/or :class:`nutils.matrix.Matrix` objects.
    '''

    warnings.deprecation('sample.eval_integrals_sparse is deprecated, use evaluable.eval_sparse instead')
    return evaluable.eval_sparse(integrals, **argdict(arguments))


def _convert(data: numpy.ndarray, inplace: bool = False) -> Union[numpy.ndarray, matrix.Matrix]:
    '''Convert a two-dimensional sparse object to an appropriate object.

    The return type is determined based on dimension: a zero-dimensional object
    becomes a scalar, a one-dimensional object a (dense) Numpy vector, a
    two-dimensional object a Nutils matrix, and any higher dimensional object a
    deduplicated and pruned sparse object.
    '''

    ndim = sparse.ndim(data)
    return sparse.toarray(data) if ndim < 2 \
        else matrix.fromsparse(data, inplace=inplace) if ndim == 2 \
        else sparse.prune(sparse.dedup(data, inplace=inplace), inplace=True)


class _Integral(function.Array):

    def __init__(self, integrand: function.Array, sample: Sample) -> None:
        self._integrand = integrand
        self._sample = sample
        super().__init__(shape=integrand.shape, dtype=float if integrand.dtype in (bool, int) else integrand.dtype, spaces=integrand.spaces - frozenset(sample.spaces), arguments=integrand.arguments)

    def lower(self, args: function.LowerArgs) -> evaluable.Array:
        ielem = evaluable.loop_index('_sample_' + '_'.join(self._sample.spaces), self._sample.nelems)
        weights = self._sample.get_evaluable_weights(ielem)
        integrand = self._integrand.lower(args | self._sample.get_lower_args(ielem))
        elem_integral = evaluable.einsum('B,ABC->AC', weights, integrand, B=weights.ndim, C=self.ndim)
        return evaluable.loop_sum(elem_integral, ielem)


class _ConcatenatePoints(function.Array):

    def __init__(self, func: function.Array, sample: _TransformChainsSample) -> None:
        self._func = func
        self._sample = sample
        super().__init__(shape=(sample.npoints, *func.shape), dtype=func.dtype, spaces=func.spaces - frozenset(sample.spaces), arguments=func.arguments)

    def lower(self, args: function.LowerArgs) -> evaluable.Array:
        axis = len(args.points_shape)
        ielem = evaluable.loop_index('_sample_' + '_'.join(self._sample.spaces), self._sample.nelems)
        args |= self._sample.get_lower_args(ielem)
        func = self._func.lower(args)
        func = evaluable.Transpose.to_end(func, *range(axis, len(args.points_shape)))
        for i in range(len(args.points_shape) - axis - 1):
            func = evaluable.Ravel(func)
        func = evaluable.loop_concatenate(func, ielem)
        return evaluable.Transpose.from_end(func, axis)


class _ReorderPoints(function.Array):

    def __init__(self, func: function.Array, indices: evaluable.Array) -> None:
        self._func = func
        self._indices = indices
        assert indices.ndim == 1 and func.shape[0] == indices.shape[0].__index__()
        super().__init__(shape=func.shape, dtype=func.dtype, spaces=func.spaces, arguments=func.arguments)

    def lower(self, args: function.LowerArgs) -> evaluable.Array:
        func = self._func.lower(args)
        axis = len(args.points_shape)
        return evaluable.Transpose.from_end(evaluable.Inflate(evaluable.Transpose.to_end(func, axis), self._indices, self._indices.shape[0]), axis)


class _Basis(function.Array):

    def __init__(self, sample: _TransformChainsSample, interpolation: str) -> None:
        self._sample = sample
        if interpolation not in ('none', 'nearest'):
            raise ValueError(f'invalid interpolation {interpolation!r}; valid values are "none" and "nearest"')
        self._interpolation = interpolation
        super().__init__(shape=(sample.npoints,), dtype=float, spaces=frozenset({sample.space}), arguments={})

    def lower(self, args: function.LowerArgs) -> evaluable.Array:
        aligned_space_coords = args.coordinates[self._sample.space]
        assert aligned_space_coords.ndim == len(args.points_shape) + 1
        space_coords, where = evaluable.unalign(aligned_space_coords)
        # Reinsert the coordinate axis, the last axis of `aligned_space_coords`, or
        # make sure this is the last axis of `space_coords`.
        if len(args.points_shape) not in where:
            space_coords = evaluable.InsertAxis(space_coords, aligned_space_coords.shape[-1])
            where += len(points_shape),
        elif where[-1] != len(args.points_shape):
            space_coords = evaluable.Transpose(space_coords, numpy.argsort(where))
            where = tuple(sorted(where))

        (chain, *_), tip_index = args.transform_chains[self._sample.space]
        index = evaluable.TransformIndex(self._sample.transforms[0], chain, tip_index)
        coords = evaluable.TransformCoords(self._sample.transforms[0], chain, tip_index, space_coords)
        expect = self._sample.points.get_evaluable_coords(index)
        sampled = evaluable.Sampled(coords, expect, self._interpolation)
        indices = self._sample.get_evaluable_indices(index)
        basis = evaluable.Inflate(sampled, dofmap=indices, length=evaluable.constant(self._sample.npoints))

        # Realign the points axes. The coordinate axis of `aligned_space_coords` is
        # replaced by a dofs axis in the aligned basis, hence we can reuse `where`.
        return evaluable.align(basis, where, (*args.points_shape, evaluable.constant(self._sample.npoints)))


def _offsets(pointsseq: PointsSequence) -> evaluable.Array:
    ielem = evaluable.loop_index('_ielem', len(pointsseq))
    npoints, ndims = pointsseq.get_evaluable_coords(ielem).shape
    return evaluable._SizesToOffsets(evaluable.loop_concatenate(evaluable.InsertAxis(npoints, evaluable.constant(1)), ielem))

# vim:sw=4:sts=4:et
