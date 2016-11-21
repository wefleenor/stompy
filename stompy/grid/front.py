"""

An advancing front grid generator for use with unstructured_grid

Largely a port of paver.py.

"""
import unstructured_grid
import exact_delaunay
import numpy as np
from scipy import optimize as opt

import utils
import logging
log=logging.getLogger(__name__)

try:
    import matplotlib.pyplot as plt
except ImportError:
    log.warning("Plotting not available - no matplotlib")
    plt=None


# copied from paver verbatim, with edits to reference
# numpy identifiers via np._
def one_point_cost(pnt,edges,target_length=5.0):
    # pnt is intended to complete a triangle with each
    # pair of points in edges, and should be to the left
    # of each edge
    penalty = 0
    
    max_angle = 85.0*np.pi/180.

    # all_edges[triangle_i,{ab,bc,ca},{x,y}]
    all_edges = np.zeros( (edges.shape[0], 3 ,2), np.float64 )
    
    # get the edges:
    all_edges[:,0,:] = edges[:,0] - pnt  # ab
    all_edges[:,1,:] = edges[:,1] - edges[:,0] # bc
    all_edges[:,2,:] = pnt - edges[:,1] # ca

    i = np.arange(3)
    im1 = (i-1)%3
    
    #--# cost based on angle:
    abs_angles = np.arctan2( all_edges[:,:,1], all_edges[:,:,0] )
    all_angles = (np.pi - (abs_angles[:,i] - abs_angles[:,im1]) % (2*np.pi)) % (2*np.pi)
        
    # a_angles = (pi - (ab_angles - ca_angles) % (2*pi)) % (2*pi)
    # b_angles = (pi - (bc_angles - ab_angles) % (2*pi)) % (2*pi)
    # c_angles = (pi - (ca_angles - bc_angles) % (2*pi)) % (2*pi)

    if 1:
        # 60 is what it's been for a while, but I think in one situation
        # this put too much weight on small angles.
        # tried considering just large angles, but that quickly blew up.
        # even just changing this to 50 still blows up.
        #  how about a small tweak - s/60/58/ ??
        worst_angle = np.abs(all_angles - 60*np.pi/180.).max() 
        alpha = worst_angle /(max_angle - 60*np.pi/180.0)

        # 10**alpha: edges got very short...
        # 5**alpha - 1: closer, but overall still short edges.
        # alpha**5: angles look kind of bad
        angle_penalty = 10*alpha**5

        # Seems like it doesn't try hard enough to get rid of almost bad angles.
        # in one case, there is a small angle of 33.86 degrees, and another angle
        # of 84.26 degrees. so the cost function only cares about the small angle
        # because it is slightly more deviant from 60deg, but we may be in a cell
        # where the only freedom we have is to change the larger angles.

        # so add this in:
        if 1:
            # extra exponential penalty for nearly bad triangles:
            # These values mean that 3 degrees before the triangle is invalid
            # the exponential cuts in and will add a factor of e by the time the
            # triangles is invalid.

            scale_rad = 3.0*np.pi/180. # radians - e-folding scale of the cost
            # max_angle - 2.0*scale_rad works..
            thresh = max_angle - 1.0*scale_rad # angle at which the exponential 'cuts in'
            big_angle_penalty = np.exp( (all_angles.max() - thresh) / scale_rad)
    else:
        alphas = (all_angles - 60*np.pi/180.) / (max_angle - 60*np.pi/180.)
        alphas = 10*alphas**4
        angle_penalty = alphas.sum()
    
    penalty += angle_penalty + big_angle_penalty

    #--# Length penalties:
    ab_lens = (all_edges[:,0,:]**2).sum(axis=1)
    ca_lens = (all_edges[:,2,:]**2).sum(axis=1)

    # the usual..
    min_len = min(ab_lens.min(),ca_lens.min())
    max_len = max(ab_lens.min(),ca_lens.min())

    undershoot = target_length**2 / min_len
    overshoot  = max_len / target_length**2

    length_penalty = 0

    length_factor = 2
    length_penalty += length_factor*(max(undershoot,1) - 1)
    length_penalty += length_factor*(max(overshoot,1) - 1)

    # paver had two other approachs, effectively commented out
    penalty += length_penalty

    return penalty
    

class Curve(object):
    """
    Boundaries which can be open or closed, indexable
    by a floating point value (including modulo arithmetic).
    By default, indexes by distance along each segment.
    """
    class CurveException(Exception):
        pass
    
    def __init__(self,points,closed=True):
        self.points=np.asarray(points)
        self.closed=closed
        if self.closed:
            self.points = np.concatenate( (self.points,
                                           self.points[:1,:] ) )
        
        self.distances=utils.dist_along(self.points)
    def __call__(self,f,metric='distance'):
        if metric=='distance':
            if self.closed:
                # wraps around
                f=f % self.distances[-1]
            # side='right' ensures that f=0 works
            idxs=np.searchsorted(self.distances,f,side='right') - 1
            
            alphas = (f - self.distances[idxs]) / (self.distances[idxs+1]-self.distances[idxs])
            if not np.isscalar(alphas):
                alphas = alphas[:,None]
            return (1-alphas)*self.points[idxs] + alphas*self.points[idxs+1]
        else:
            assert False
    def total_distance(self):
        return self.distances[-1]

    def upsample(self,scale,return_sources=False):
        """
        return_sources: return a second array having the distance values for each
          return point, if this is true.
        """
        # def upsample_linearring(points,density,closed_ring=1,return_sources=False):
        new_segments = []
        sources = []

        for i,(A,B) in enumerate(zip( self.points[:-1,:],
                                      self.points[1:,:] ) ):
            l = utils.dist(B-A)
            local_scale = scale( 0.5*(A+B) )

            npoints = max(1,round( l/local_scale ))
            alphas = np.arange(npoints) / float(npoints)
            alphas=alphas[:,None]
            
            new_segment = (1.0-alphas)*A + alphas*B
            new_segments.append(new_segment)
            if return_sources:
                sources.append(self.distances[i] + alphas*l)

        new_points = np.concatenate( new_segments )

        if return_sources:
            sources = np.concatenate(sources)
            return new_points,sources
        else:
            return new_points

    def distance_away(self,anchor_f,signed_distance,rtol=0.05):
        """  Find a point on the curve signed_distance away from the
        point corresponding to anchor_f, within the given relative tolerance.
        returns new_f,new_x.

        If a point could not be found within the requested tolerance, raises
        a self.CurveException.

        Starting implementation is weak - it ignores any knowledge of the piecewise
        linear geometry.  Will need to be amended to take that into account, since 
        in its current state it will succumb to local minima/maxima.
        """
        anchor_x = self(anchor_f)
        offset=signed_distance
        direc=np.sign(signed_distance)
        target_d=np.abs(signed_distance)

        last_offset=0.0
        last_d=0.0

        # first loop to bracket the distance:
        for step in range(10): # ad-hoc limit to avoid bad juju.
            new_x=self(anchor_f + offset)
            d=utils.dist(anchor_x-new_x)
            rel_err=(d - target_d)/target_d
            if -rtol < rel_err < rtol:
                return anchor_f + offset,new_x
            if rel_err<0:
                if d<last_d:
                    # this could easily be a local minimum - as this becomes important,
                    # then it would be better to include some bounds checking, and only
                    # fail when we can prove that no solution exists.
                    raise self.CurveException("Distance got smaller - need to be smarter")
                last_offset=offset
                last_d=d
                offset*=1.5
                continue
            else:
                break # can binary search
        # binary search
        low_offset=last_offset
        high_offset=offset
        for step in range(10):
            mid_offset = 0.5*(low_offset + high_offset)
            mid_x = self(anchor_f + mid_offset)
            mid_d = utils.dist(anchor_x - mid_x)
            rel_err=(mid_d - target_d)/target_d
            if -rtol<rel_err<rtol:
                return anchor_f+mid_offset,mid_x
            elif mid_d < target_d:
                low_offset=mid_offset
            else:
                high_offset=mid_offset
        else:
            raise self.CurveException("Binary search failed")

    def is_forward(self,fa,fb,fc):
        d=self.total_distance()
        return ((fb-fa) % d) < ((fc-fa)%d)
    def is_reverse(self,fa,fb,fc):
        return self.is_forward(fc,fb,fa)
    
    def plot(self,ax=None,**kw):
        ax=ax or plt.gca()
        return ax.plot(self.points[:,0],self.points[:,1],**kw)[0]
        

def internal_angle(A,B,C):
    BA=A-B
    BC=C-B
    theta_BA = np.arctan2( BA[1], BA[0] )
    theta_BC = np.arctan2( BC[1], BC[0] )
    return (theta_BA - theta_BC) % (2*np.pi)

class StrategyFailed(Exception):
    pass

class Strategy(object):
    def metric(self,site,scale_factor):
        assert False
    def execute(self,site):
        """
        Apply this strategy to the given Site.
        Returns a dict with nodes,cells which were modified 
        """
        assert False

class WallStrategy(Strategy):
    """ 
    Add two edges and a new triangle to the forward side of the
    site.
    """
    def metric(self,site):
        # rough translation from paver
        theta=site.internal_angle * 180/np.pi
        scale_factor = site.edge_length / site.local_length

        # Wall can be applied in a wide variety of situations
        # angles greater than 90, Wall may be the only option
        # angles less than 60, and we can't do a wall.
        return np.clip( (90 - theta) / 30,
                        0,1)

    def execute(self,site):
        na,nb,nc= site.abc
        grid=site.grid
        b,c = grid.nodes['x'][ [nb,nc] ]
        bc=c-b
        new_x = b + utils.rot(np.pi/3,bc)
        nd=grid.add_node(x=new_x,fixed=site.af.FREE)

        # new_c=grid.add_cell_and_edges( [nb,nc,nd] )
        j0=grid.nodes_to_edge(nb,nc)
        unmesh2=[grid.UNMESHED,grid.UNMESHED]
        # the correct unmeshed will get overwritten in
        # add cell.
        j1=grid.add_edge(nodes=[nc,nd],cells=unmesh2)
        j2=grid.add_edge(nodes=[nb,nd],cells=unmesh2)
        new_c=grid.add_cell(nodes=[nb,nc,nd],
                            edges=[j0,j1,j2])

        return {'nodes': [nd],
                'cells': [new_c] }

class CutoffStrategy(Strategy):
    def metric(self,site):
        theta=site.internal_angle
        scale_factor = site.edge_length / site.local_length

        # Cutoff wants a small-ish internal angle
        # If the sites edges are long, scale_factor > 1
        # and we'd like to be making smaller edges, so ideal angle gets smaller
        # 
        if theta> 89*np.pi/180:
            return np.inf # not allowed
        else:
            ideal=60 + (1-scale_factor)*30
            return np.abs(theta - ideal*np.pi/180.)
    def execute(self,site):
        grid=site.grid
        na,nb,nc=site.abc
        j0=grid.nodes_to_edge(na,nb)
        j1=grid.nodes_to_edge(nb,nc)
        j2=grid.nodes_to_edge(nc,na)
        if j2 is None:
            # typical, but if we're finishing off the last triangle, this edge
            # exists.
            j2=grid.add_edge(nodes=[nc,na],cells=[grid.UNMESHED,grid.UNMESHED])
        
        c=site.grid.add_cell(nodes=site.abc,
                             edges=[j0,j1,j2])
        
        return {'cells':[c] }

class JoinStrategy(Strategy):
    """ 
    Given an inside angle, merge the two edges
    """
    def metric(self,site):
        theta=site.internal_angle
        scale_factor = site.edge_length / site.local_length

        # Cutoff wants a small-ish internal angle
        # If the sites edges are long, scale_factor > 1
        # and we'd like to be making smaller edges, so ideal angle gets smaller
        # 
        if theta> 89*np.pi/180:
            return np.inf # not allowed
        else:
            # as theta goes to 0, a Join has no effect on scale.
            # at larger theta, a join effectively coarsens
            # so if edges are too small, we want to coarsen, scale_factor
            # will be < 1
            return scale_factor * theta
    def execute(self,site):
        grid=site.grid
        na,nb,nc=site.abc

        # choose the node to move -
        mover=None

        j_ac=grid.nodes_to_edge(na,nc)
        if j_ac is None:
            if grid.nodes['fixed'][na]!=site.af.FREE:
                if grid.nodes['fixed'][nc]!=site.af.FREE:
                    raise StrategyFailed("Neither node is movable, cannot Join")
                mover=nc
                anchor=na
            else:
                mover=na
                anchor=nc
        else:
            # special case: nodes are already joined, but there is no
            # cell.
            # this *could* be extended to allow the deletion of thin cells,
            # but I don't want to get into that yet (since it's modification,
            # not creation)
            if (grid.edges['cells'][j_ac,0] >=0) or (grid.edges['cells'][j_ac,1]>=0):
                raise StrategyFailed("Edge already has real cells")
            if grid.nodes['fixed'][na] in [site.af.FREE,site.af.SLIDE]:
                mover=na
                anchor=nc
            elif grid.nodes['fixed'][nc] in [site.af.FREE,site.af.SLIDE]:
                mover=nc
                anchor=na
            else:
                raise StrategyFailed("Neither node can be moved")

            grid.delete_edge(j_ac)

        edits={'cells':[],'edges':[] }

        cells_to_replace=[]
        def archive_cell(c):
            cells_to_replace.append( (c,grid.cells[c].copy()) )
            grid.delete_cell(c)

        edges_to_replace=[]
        def archive_edge(j):
            for c in grid.edges['cells'][j]:
                if c>=0:
                    archive_cell(c)

            edges_to_replace.append( (j,grid.edges[j].copy()) )
            grid.delete_edge(j)

        for j in list(grid.node_to_edges(mover)):
            archive_edge(j)
        grid.delete_node(mover)

        for j,data in edges_to_replace:
            nodes=data['nodes']
            
            for i in [0,1]:
                if nodes[i]==mover:
                    if nodes[1-i]==nb:
                        break
                    else:
                        nodes[i]=anchor
            else:
                # need to remember boundary, but any real
                # cells get added in the next step, so can
                # be -2 here.
                cells=data['cells']
                if cells[0]>=0:
                    cells[0]=-2
                if cells[1]>=0:
                    cells[1]=-2

                jnew=grid.add_edge( nodes=nodes, cells=cells )
                edits['edges'].append(jnew)

        for c,data in cells_to_replace:
            nodes=data['nodes']
            for ni,n in enumerate(nodes):
                if n==mover:
                    nodes[ni]=anchor
            cnew=grid.add_cell(nodes=nodes)
            edits['cells'].append(cnew)
        return edits
    
Wall=WallStrategy()
Cutoff=CutoffStrategy()
Join=JoinStrategy()

class Site(object):
    """
    represents a potential location for advancing the front.
    """
    def __init__(self):
        pass
    def metric(self):
        """ Smaller number means more likely to be chosen.
        """
        assert False
    def actions(self):
        return []
        
class TriangleSite(object):
    """ 
    When adding triangles, the heuristic is to choose
    tight locations.
    """
    def __init__(self,af,nodes):
        self.af=af
        self.grid=af.grid
        assert len(nodes)==3
        self.abc = nodes
    def metric(self):
        return self.internal_angle
    def points(self):
        return self.grid.nodes['x'][ self.abc ]
    
    @property
    def internal_angle(self):
        A,B,C = self.points() 
        return internal_angle(A,B,C)
    @property
    def edge_length(self):
        return utils.dist( np.diff(self.points(),axis=0) ).mean()
    
    @property
    def local_length(self):
        scale = self.af.scale
        return scale( self.points().mean(axis=0) )

    def plot(self,ax=None):
        ax=ax or plt.gca()
        points=self.grid.nodes['x'][self.abc]
        return ax.plot( points[:,0],points[:,1],'r-o' )[0]
    def actions(self):
        theta=self.internal_angle
        return [Wall,Cutoff,Join]


class ShadowCDT(exact_delaunay.Triangulation):
    """ Tracks modifications to an unstructured grid and
    maintains a shadow representation with a constrained Delaunay
    triangulation, which can be used for geometric queries and
    predicates.
    """
    def __init__(self,g):
        super(ShadowCDT,self).__init__(extra_node_fields=[('g_n','i4')])
        self.g=g
        
        self.nodemap_g_to_local={}

        g.subscribe_before('add_node',self.before_add_node)
        g.subscribe_after('add_node',self.after_add_node)
        g.subscribe_before('modify_node',self.before_modify_node)
        g.subscribe_before('delete_node',self.before_delete_node)
        
        g.subscribe_before('add_edge',self.before_add_edge)
        g.subscribe_before('delete_edge',self.before_delete_edge)
        g.subscribe_before('modify_edge',self.before_modify_edge)
        
    def before_add_node(self,g,func_name,**k):
        pass # no checks quite yet
    def after_add_node(self,g,func_name,return_value,**k):
        n=return_value
        self.nodemap_g_to_local[n]=self.add_node(x=k['x'],g_n=n)
    def before_modify_node(self,g,func_name,n,**k):
        if 'x' in k:
            my_n=self.nodemap_g_to_local[n]
            self.modify_node(my_n,x=k['x'])
    def before_delete_node(self,g,func_name,n,**k):
        self.delete_node(self.nodemap_g_to_local[n])
        del self.nodemap_g_to_local[n]
    def before_add_edge(self,g,func_name,**k):
        nodes=k['nodes']
        self.add_constraint(nodes[0],nodes[1])
    def before_modify_edge(self,g,func_name,j,**k):
        if 'nodes' not in k:
            return
        old_nodes=g.edges['nodes'][j]
        new_nodes=k['nodes']
        self.remove_constraint( old_nodes[0],old_nodes[1])
        self.add_constraint( new_nodes[0],new_nodes[1] )
    def before_delete_edge(self,g,func_name,j,**k):
        nodes=g.edges['nodes'][j]
        self.remove_constraint(nodes[0],nodes[1])

        
class AdvancingFront(object):
    """
    Implementation of advancing front
    """
    scale=None
    grid=None
    cdt=None

    # 'fixed' flags:
    #  in order of increasing degrees of freedom in its location.
    # don't use 0 here, so that it's easier to detect uninitialized values
    RIGID=1 # should not be moved at all
    SLIDE=2 # able to slide along a ring
    FREE=3  # not constrained 
    
    def __init__(self,grid=None,scale=None):
        """
        """
        self.log = logging.getLogger("AdvancingFront")

        if grid is None:
            grid=unstructured_grid.UnstructuredGrid()
        self.grid = self.instrument_grid(grid)

        self.curves=[]
        
    def add_curve(self,curve):
        self.curves.append( curve )
        return len(self.curves)-1

    def set_edge_scale(self,scale):
        self.scale=scale
        
    def instrument_grid(self,g):
        """
        Add fields to the given grid to support advancing front
        algorithm.  Modifies grid in place, and returns it.

        Also creates a Triangulation which follows modifications to 
        the grid, keeping a constrained Delaunay triangulation around.
        """
        g.add_node_field('oring',-1*np.ones(g.Nnodes(),'i4'),on_exists='pass')
        g.add_node_field('fixed',np.zeros(g.Nnodes(),'i4'),on_exists='pass')
        g.add_node_field('ring_f',-1*np.ones(g.Nnodes(),'f8'),on_exists='pass')

        # Subscribe to operations *before* they happen, so that the constrained
        # DT can signal that an invariant would be broken
        self.cdt=ShadowCDT(g)
                          
        return g
    
    def initialize_boundaries(self):
        for curve_i,curve in enumerate(self.curves):
            curve_points,srcs=curve.upsample(self.scale,return_sources=True)

            # add the nodes in:
            nodes=[self.grid.add_node(x=curve_points[j],
                                      oring=curve_i,
                                      ring_f=srcs[j],
                                      fixed=self.SLIDE)
                   for j in range(len(curve_points))]

            if curve.closed:
                Ne=len(curve_points)
            else:
                Ne=len(curve_points) - 1

            pairs=zip( np.arange(Ne),
                       (np.arange(Ne)+1)%Ne)
            for na,nb in pairs:
                self.grid.add_edge( nodes=[nodes[na],nodes[nb]],
                                    cells=[self.grid.UNMESHED,
                                           self.grid.UNDEFINED] )

    def choose_site(self):
        sites=[]
        valid=(self.grid.edges['cells'][:,:]==self.grid.UNMESHED) 
        J,Orient = np.nonzero(valid)

        for j,orient in zip(J,Orient):
            if self.grid.edges['deleted'][j]:
                continue
            he=self.grid.halfedge(j,orient)
            he_nxt=he.fwd()
            a=he.node_rev()
            b=he.node_fwd()
            bb=he_nxt.node_rev()
            c=he_nxt.node_fwd()
            assert b==bb

            sites.append( TriangleSite(self,nodes=[a,b,c]) )
        if len(sites):
            scores=[ site.metric()
                     for site in sites ]
            best=np.argmin( scores ) 
            return sites[best]
        else:
            return None
        
    def free_span(self,he,max_span,direction):
        span=0.0
        if direction==1:
            trav=he.node_fwd()
            last=anchor=he.node_rev()
        else:
            trav=he.node_rev()
            last=anchor=he.node_fwd()

        def pred(n):
            return ( (self.grid.nodes['fixed'][n]== self.SLIDE) and
                     len(self.grid.node_to_edges(n))<=2 )

        while pred(trav) and (trav != anchor) and (span<max_span):
            span += utils.dist( self.grid.nodes['x'][last] -
                                 self.grid.nodes['x'][trav] )
            if direction==1:
                he=he.fwd()
                last,trav = trav,he.node_fwd()
            elif direction==-1:
                he=he.rev()
                last,trav = trav,he.node_rev()
            else:
                assert False
        return span
    
    max_span_factor=4     
    def resample(self,n,anchor,scale,direction):
        self.log.debug("resample %d to be  %g away from %d in the %s direction"%(n,scale,anchor,
                                                                                 direction) )
        if direction==1: # anchor to n is t
            he=self.grid.nodes_to_halfedge(anchor,n)
        elif direction==-1:
            he=self.grid.nodes_to_halfedge(n,anchor)
        else:
            assert False

        span_length = self.free_span(he,self.max_span_factor*scale,direction)
        self.log.debug("free span from the anchor is %g"%span_length)

        if span_length < self.max_span_factor*scale:
            n_segments = max(1,round(span_length / scale))
            target_span = span_length / n_segments
            if n_segments==1:
                self.log.debug("Only one space for 1 segment")
                return
        else:
            target_span=scale

        # first, find a point on the original ring which satisfies the target_span
        oring=self.grid.nodes['oring'][anchor]
        curve = self.curves[oring]
        anchor_f = self.grid.nodes['ring_f'][anchor]
        try:
            new_f,new_x = curve.distance_away(anchor_f,direction*target_span)
        except curve.CurveException as exc:
            raise

        # check to see if there are other nodes in the way, and remove them.
        nodes_to_delete=[]
        trav=he
        while True:
            if direction==1:
                trav=trav.fwd()
            else:
                trav=trav.rev()
            # we have anchor, n, and
            if trav==he:
                self.log.error("Made it all the way around!")
                raise Exception("This is probably bad")

            if direction==1:
                n_trav=trav.node_fwd()
                f_trav=self.grid.nodes['ring_f'][n_trav]
                if curve.is_forward( anchor_f, new_f, f_trav ):
                    break
            else:
                n_trav=trav.node_rev()
                f_trav=self.grid.nodes['ring_f'][n_trav]
                if curve.is_reverse( anchor_f, new_f, f_trav ):
                    break

            nodes_to_delete.append(n_trav)

        for d in nodes_to_delete:
            self.grid.merge_edges(node=d)

        self.grid.modify_node(n,x=new_x,ring_f=new_f)

    def resample_neighbors(self,site):
        a,b,c = site.abc
        local_length = self.scale( site.points().mean(axis=0) )

        for n,direction in [ (a,-1),
                             (c,1) ]:
            if ( (self.grid.nodes['fixed'][n] == self.SLIDE) and
                 len(self.grid.node_to_edges(n))<=2 ):
                self.resample(n=n,anchor=b,scale=local_length,direction=direction)

    def cost_function(self,n):
        local_length = self.scale( self.grid.nodes['x'][n] )
        my_cells = self.grid.node_to_cells(n)

        if len(my_cells) == 0:
            return None

        cell_nodes = [self.grid.cell_to_nodes(c)
                      for c in my_cells ]

        # for the moment, can only deal with triangles
        cell_nodes=np.array(cell_nodes)

        # pack our neighbors from the cell list into an edge
        # list that respects the CCW condition that pnt must be on the
        # left of each segment
        for j in range(len(cell_nodes)):
            if cell_nodes[j,0] == n:
                cell_nodes[j,:2] = cell_nodes[j,1:]
            elif cell_nodes[j,1] == n:
                cell_nodes[j,1] = cell_nodes[j,0]
                cell_nodes[j,0] = cell_nodes[j,2]

        edges = cell_nodes[:,:2]
        edge_points = self.grid.nodes['x'][edges]

        def cost(x,edge_points=edge_points,local_length=local_length):
            return one_point_cost(x,edge_points,target_length=local_length)

        return cost

    def eval_cost(self,n):
        fn=self.cost_function(n)
        return fn and fn(self.grid.nodes['x'][n])

    def optimize_nodes(self,nodes,max_levels=3,cost_thresh=2):
        max_cost=0

        for level in range(max_levels):
            for n in nodes:
                max_cost=max(max_cost,self.relax_node(n))
            if max_cost <= cost_thresh:
                break
            if level==0:
                # just try re-optimizing once
                pass
            else:
                pass
                # expand list of nodes one level

    def optimize_edits(self,edits,**kw):
        """
        Given a set of elements (which presumably have been modified
        and need tuning), jostle nodes around to improve the cost function
        """
        nodes = edits.get('nodes',[])
        for c in edits.get('cells',[]):
            for n in self.grid.cell_to_nodes(c):
                if n not in nodes:
                    nodes.append(n)
        return self.optimize_nodes(nodes,**kw)

    def relax_node(self,n):
        """ Move node n, subject to its constraints, to minimize
        the cost function.  Return the final value of the cost function
        """
        self.log.debug("Relaxing node %d"%n)
        if self.grid.nodes['fixed'][n] == self.FREE:
            return self.relax_free_node(n)
        elif self.grid.nodes['fixed'][n] == self.SLIDE:
            return self.relax_slide_node(n)

    def relax_free_node(self,n):
        cost=self.cost_function(n)
        if cost is None:
            return None
        x0=self.grid.nodes['x'][n]
        local_length=self.scale( x0 )
        new_x = opt.fmin(cost,
                         x0,
                         xtol=local_length*1e-4,
                         disp=0)
        dx=utils.dist( new_x - x0 )
        self.log.info('Relaxation moved node %f'%dx)
        if dx !=0.0:
            self.grid.modify_node(n,x=new_x)
        return cost(new_x)

    def relax_slide_node(self,n):
        cost_free=self.cost_function(n)
        if cost_free is None:
            return 
        x0=self.grid.nodes['x'][n]
        f0=self.grid.nodes['ring_f'][n]
        ring=self.grid.nodes['oring'][n]

        assert np.isfinite(f0)
        assert ring>=0

        cost_slide=lambda f: cost_free( self.curves[ring](f) )

        local_length=self.scale( x0 )
        new_f = opt.fmin(cost_slide,
                         [f0],
                         xtol=local_length*1e-4,
                         disp=0)
        new_f=new_f[0]
        if new_f!=f0:
            self.slide_node(n,new_f-f0)
        return cost_slide(new_f)

    def find_slide_conflicts(self,n,delta_f):
        n_ring=self.grid.nodes['oring'][n]
        n_f=self.grid.nodes['ring_f'][n]
        new_f=n_f + delta_f
        curve=self.curves[n_ring]
        # Want to find edges in the direction of travel
        # it's a little funny to use half-edges, since what
        # really care about is what it's facing
        # would like to use half-edges here, but it's not entirely
        # well-defined, so rather than introduce some future pitfalls,
        # do things a bit more manually.
        to_delete=[]
        for nbr in self.grid.node_to_nodes(n):
            if self.grid.nodes['oring'][nbr]!=n_ring:
                continue

            nbr_f=self.grid.nodes['ring_f'][nbr]
            if self.grid.node_degree(nbr)!=2:
                continue

            if delta_f>0:
                # either the nbr is outside our slide area, or could
                # be in the opposite direction along the ring
                if curve.is_forward(n_f,n_f+delta_f,nbr_f):
                    continue
                to_delete.append(nbr)
                he=self.grid.nodes_to_halfedge(n,nbr)
                while 1:
                    he=he.fwd()
                    nbr=he.node_fwd()
                    nbr_f=self.grid.nodes['ring_f'][nbr]
                    if curve.is_forward(n_f,n_f+delta_f,nbr_f):
                        break
                    to_delete.append(nbr)
                break
            else:
                if curve.is_reverse(n_f,n_f+delta_f,nbr_f):
                    continue
                to_delete.append(nbr)
                he=self.grid.nodes_to_halfedge(nbr,n)
                while 1:
                    he=he.rev()
                    nbr=he.node_rev()
                    nbr_f=self.grid.nodes['ring_f'][nbr]
                    if curve.is_reverse(n_f,n_f+delta_f,nbr_f):
                        break
                    to_delete.append(nbr)
                break
        # sanity checks:
        for nbr in to_delete:
            assert n_ring==self.grid.nodes['oring'][nbr]
            # For now, depart a bit from paver, and rather than
            # having HINT nodes, HINT and SLIDE are both fixed=SLIDE,
            # but differentiate based on node degree.
            assert self.grid.nodes['fixed'][nbr]==self.SLIDE
            assert self.grid.node_degree(nbr)==2
        return to_delete
    
    def slide_node(self,n,delta_f):
        conflicts=self.find_slide_conflicts(n,delta_f)
        for nbr in conflicts:
            self.grid.merge_edges(node=nbr)

        n_ring=self.grid.nodes['oring'][n]
        n_f=self.grid.nodes['ring_f'][n]
        new_f=n_f + delta_f
        curve=self.curves[n_ring]

        self.grid.modify_node(n,x=curve(new_f),ring_f=new_f)

    def loop(self,count=0):
        while 1:
            site=self.choose_site()
            if site is None:
                break
            self.resample_neighbors(site)
            actions=site.actions()
            metrics=[a.metric(site) for a in actions]
            best=np.argmin(metrics)
            edits=actions[best].execute(site)
            self.optimize_edits(edits)
            count-=1
            if count==0:
                break
        
    zoom=None
    def plot_summary(self,ax=None,
                     label_nodes=True):
        ax=ax or plt.gca()
        ax.cla()

        self.curves[0].plot(ax=ax,color='0.5',zorder=-5)
        self.grid.plot_edges(ax=ax)
        if label_nodes:
            labeler=lambda ni,nr: str(ni)
        else:
            labeler=None
        self.grid.plot_nodes(ax=ax,labeler=labeler)
        ax.axis('equal')
        if self.zoom:
            ax.axis(self.zoom)