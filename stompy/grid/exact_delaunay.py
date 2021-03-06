# A pure-python, exact delaunay triangulation.
# uses robust_predicates for in-circle tests, follows
# the algorithm of CGAL to the extent possible.
import logging
import pdb
logger = logging.getLogger()

import six
import numpy as np
import matplotlib.pyplot as plt

# do these work in py2?
from ..spatial import robust_predicates
from . import unstructured_grid
from ..utils import circular_pairs

try:
    from scipy import spatial
except ImportError:
    spatial=None

class DuplicateNode(Exception):
    pass

class BadConstraint(Exception):
    pass

class IntersectingConstraints(BadConstraint):
    pass
class ConstraintCollinearNode(BadConstraint):
    pass

class Triangulation(unstructured_grid.UnstructuredGrid):
    """ 
    Mimics the Triangulation_2 class of CGAL.
    note that we make some additional assumptions on invariants -
     nodes, cells and edges are ordered in a consistent way:
    
    """ 
    INF_NODE=-666
    INF_CELL=unstructured_grid.UnstructuredGrid.UNMESHED
    max_sides=3

    # local exception types
    DuplicateNode=DuplicateNode
    IntersectingConstraints=IntersectingConstraints
    BadConstraint=BadConstraint
    ConstraintCollinearNode=ConstraintCollinearNode

    post_check=False # enables [expensive] checks after operations
    
    edge_dtype=(unstructured_grid.UnstructuredGrid.edge_dtype +
                [ ('constrained',np.bool8) ] )

    def add_node(self,**kwargs):
        # will eventually need some caching or indexing to make
        # the locate faster.  locate() happens first so that 
        # the mesh complies with invariants and doesn't have a dangling
        # node
        loc=self.locate(kwargs['x'])

        n=super(Triangulation,self).add_node(**kwargs)

        self.tri_insert(n,loc)
        return n

    def modify_node(self,n,**kwargs):
        if 'x' not in kwargs:
            return super(Triangulation,self).modify_node(n,**kwargs)
        old_rec=self.nodes[n]
        # Brute force, removing and re-adding, is no good as the
        # constraints are lost.
        # A slightly more refined, but still brutish, approach, is to save
        # the constraints, delete, add, add constraints.
        # be sped up

        # but adding the constraints back can fail, in which case we should
        # roll back our state, and fire an exception.

        constraints_to_replace=[]
        for j in self.node_to_edges(n):
            if self.edges['constrained'][j]:
                constraints_to_replace.append( self.edges['nodes'][j].copy() )

        old_x=self.nodes['x'][n].copy() # in case of rollback
        
        self.delete_node(n)
        
        for fld in old_rec.dtype.names:
            if fld not in ['x','deleted'] and fld not in kwargs:
                kwargs[fld]=old_rec[fld]
        new_n=self.add_node(_index=n,**kwargs)

        try:
            for n1,n2 in constraints_to_replace:
                self.add_constraint(n1,n2) # This can fail!
        except self.IntersectingConstraints as exc:
            self.log.warning("modify_node: intersecting constraints - rolling back")
            self.delete_node(n)
            kwargs['x']=old_x # move it back to where it started
            new_n=self.add_node(_index=n,**kwargs)
            for n1,n2 in constraints_to_replace:
                self.add_constraint(n1,n2) # This should not fail
            # but signal to the caller that the modify failed
            raise

        assert new_n==n
        
    def add_edge(self,**kw):
        """ add-on: cells default to INF_CELL, not -1.
        """
        j=super(Triangulation,self).add_edge(**kw)
        if 'cells' not in kw:
            self.edges[j]['cells'][:]=self.INF_CELL
        return j

    def choose_start_cell(self,t=None):
        """ choose a starting cell for trying to locate where a new vertex
        should go.  May return INF_CELL if there are no valid cells.
        t: can specify a target point which may be used with a spatial index
        to speed up the query.
        """
        c=0
        try:
            while self.cells['deleted'][c]: 
                c+=1
            return c
        except IndexError:
            return self.INF_CELL
            
    IN_VERTEX=0
    IN_EDGE=2
    IN_FACE=3
    OUTSIDE_CONVEX_HULL=4
    OUTSIDE_AFFINE_HULL=5

    def dim(self):
        if len(self.cells) and not np.all(self.cells['deleted']):
            return 2
        elif len(self.edges) and not np.all(self.edges['deleted']):
            return 1
        elif len(self.nodes) and not np.all(self.nodes['deleted']):
            return 0
        else:
            return -1
    def angle_sort_adjacent_nodes(self,n,ref_nbr=None,topo=True):
        if topo:
            return self.topo_sort_adjacent_nodes(n,ref_nbr)
        else:
            return super(Triangulation,self).angle_sort_adjacent_ndoes(n,ref_nbr=ref_nbr)
        
    def topo_sort_adjacent_nodes(self,n,ref_nbr=None):
        """ like angle_sort_adjacent_nodes, but relying on topology, not geometry.
        """
        nbrs=list(self.node_to_nodes(n))

        if len(nbrs)<3:
            snbrs=nbrs
        else:
            he_nbrs = [ self.nodes_to_halfedge(n,nbr)
                        for nbr in nbrs ]

            map_next={}

            for he in he_nbrs:
                # this doesn't use angle_sort
                c=he.cell_opp()
                map_next[c] = (he.node_fwd(),he.cell())

            trav0=trav=c

            snbrs=[]
            while 1:
                #if len(snbrs)>20: # DBG
                #    pdb.set_trace()
                node,cell = map_next[trav]
                snbrs.append(node)
                trav=cell
                if trav==trav0:
                    break
            
        if ref_nbr is not None: 
            i=list(snbrs).index(ref_nbr)
            snbrs=np.roll(snbrs,-i)

        return snbrs
        
    def locate(self,t,c=None):
        """ t: [x,y] point to locate
        c: starting cell, if known

        return loc=[face,loc_type,loc_index]
        face: INF_CELL if t is not on or inside a finite cell
        loc_type: 
          OUTSIDE_AFFINE_HULL: adding this vertex will increase the dimension of the triangulation.
             empty triangulation: dim=-1
             single vertex: dim=0
             collinear edges: dim=1
             faces: dim=2
             loc_index set to current dimensionality
          OUTSIDE_CONVEX_HULL: dimensionality may still be 1 or 2.
             if the dimension is 1, then loc_index gives the nearest node
             if the dimension is 2, then loc_index gives an adjacent half-edge
          IN_VERTEX: t coincides with existing vertex, 
             if face is finite, then it's a cell containing the vertex, and loc_index
               is the index of that vertex in the cell.
             if face is INF_CELL, implies dimension<2, and loc_index gives existing node
          IN_EDGE: t is collinear with existing edge.  
             if face is finite, it is a cell containing the edge.
             loc_index is the index of the edge itself.
             face may be INF_CELL, which implies dimension<2
          IN_FACE: t is in the interior of a face. face is the containing cell. loc_index
             is not used.
        """
        c=c or self.choose_start_cell(t)

        prev=None # previous face
        # To identify the right orientation of the half-edge, remember
        # the ordering of the nodes -- this is CCW ordering from the 
        # perspective of prev
        last_nodes=None 
        last_edge=None # the edge between c and prev

        # Checks for affine hull -
        # 3rd element gives the current dimensionality of the affine hull
        if self.Nnodes_valid()==0:
            return (self.INF_CELL,self.OUTSIDE_AFFINE_HULL,-1)
        elif self.Nedges_valid()==0:
            return (self.INF_CELL,self.OUTSIDE_AFFINE_HULL,0)
        elif self.Ncells_valid()==0:
            return self.locate_1d(t,c)


        while True:
            if c==self.INF_CELL:
                #       // c must contain t in its interior
                #       lt = OUTSIDE_CONVEX_HULL;
                #       li = c->index(infinite_vertex());
                # Changed to give adjacent edge, rather than 
                # confusing loc_index=4
                #  loc=(self.INF_CELL,self.OUTSIDE_CONVEX_HULL,last_edge)
                # changed again, to give a half-edge
                # flip the order because they were in the order with respect
                # to the prev face, but now we jumped over last_edge
                he=self.nodes_to_halfedge( last_nodes[1],last_nodes[0] )
                loc=(self.INF_CELL,self.OUTSIDE_CONVEX_HULL,he)
                return loc

            p0=self.nodes['x'][self.cells['nodes'][c,0]]
            p1=self.nodes['x'][self.cells['nodes'][c,1]]
            p2=self.nodes['x'][self.cells['nodes'][c,2]]

            prev = c

            # Orientation o0, o1, o2;

            # nodes are stored in CCW order for the cell.
            # 1st edge connects first two nodes 
            # neighboring cells follow the edges

            o0 = robust_predicates.orientation(p0,p1,t)
            if o0 == -1: # CW 
                last_edge=self.cell_to_edges(c)[0]
                last_nodes=self.cells['nodes'][c,[0,1]]
                c=self.cell_to_cells(c)[0] 
                continue

            o1 = robust_predicates.orientation(p1,p2,t)
            if o1 == -1:
                last_edge=self.cell_to_edges(c)[1]
                last_nodes=self.cells['nodes'][c,[1,2]]
                c=self.cell_to_cells(c)[1] 
                continue

            o2 = robust_predicates.orientation(p2,p0,t)
            if o2 == -1:
                last_edge=self.cell_to_edges(c)[2] 
                last_nodes=self.cells['nodes'][c,[2,0]]
                c=self.cell_to_cells(c)[2] 
                continue

            # must be in or on a face --
            break
        # For simplicity, I'm skipping some optimizations which avoid re-checking
        # the previous edge.  see Triangulation_2.h:2616

        # now t is in c or on its boundary
        o_sum=(o0==0)+(o1==0)+(o2==0)

        if o_sum==0:
            loc=(c,self.IN_FACE,4)
        elif o_sum==1:
            if o0==0:
                j=0
            elif o1==0:
                j=1
            else:
                j=2
            # better to consistently return the edge index here, not
            # just its index in the cell
            loc=(c,self.IN_EDGE,self.cells['edges'][c,j])
        elif o_sum==2:
            if o0!=0:
                loc=(c,self.IN_VERTEX,2)
            elif o1!=0:
                loc=(c,self.IN_VERTEX,0)
            else:
                loc=(c,self.IN_VERTEX,1)
        else:
            assert False
        return loc

    def locate_1d(self,t,c):
        # There are some edges, and t may fall within an edge, off the end,
        # or off to the side.
        j=six.next(self.valid_edge_iter())
        
        p0=self.nodes['x'][ self.edges['nodes'][j,0] ]
        p1=self.nodes['x'][ self.edges['nodes'][j,1] ]
        
        o=robust_predicates.orientation(p0,p1,t)
        if o!=0:
            return (self.INF_CELL,self.OUTSIDE_AFFINE_HULL,1)

        # t is collinear - need to find out whether it's in an edge
        # or not

        # choose a coordinate which varies along the line
        if p0[0]!=p1[0]:
            coord=0
        else:
            coord=1

        if (t[coord]<p0[coord]) != (t[coord]<p1[coord]):
            return (self.INF_CELL,self.IN_EDGE,j)

        # do we need to go towards increasing or decreasing coord?
        if (t[coord]<p0[coord]) and (t[coord]<p1[coord]):
            direc=-1
        else:
            direc=1
        
        while True:
            # j indexes the edge we just tested. 
            # p0 and p1 are the endpoints of the edge
            # 1. do we want a neighbor of n0 or n1?
            if direc*cmp(p0[coord],p1[coord]) < 0: # want to go towards p1
                n_adj=self.edges['nodes'][j,1]
            else:
                n_adj=self.edges['nodes'][j,0]
            for jnext in self.node_to_edges(n_adj):
                if jnext!=j:
                    j=jnext
                    break
            else:
                # walked off the end of the line -
                # n_adj is the nearest to us
                return (self.INF_CELL,self.OUTSIDE_CONVEX_HULL,n_adj)

            p0=self.nodes['x'][ self.edges['nodes'][j,0] ]
            p1=self.nodes['x'][ self.edges['nodes'][j,1] ]

            if (t[coord]<p0[coord]) != (t[coord]<p1[coord]):
                return (self.INF_CELL,self.IN_EDGE,j)

    def tri_insert(self,n,loc):
        # n: index for newly inserted node.
        # note that loc must already be computed -

        # types of inserts:
        #   on an edge, inside a face, outside the convex hull
        #   outside affine hull

        loc_c,loc_type,loc_idx = loc
        if loc_type==self.IN_FACE:
            self.tri_insert_in_face(n,loc)
        elif loc_type==self.IN_EDGE:
            self.tri_insert_in_edge(n,loc)
        elif loc_type==self.IN_VERTEX:
            raise DuplicateNode()
        elif loc_type==self.OUTSIDE_CONVEX_HULL:
            self.tri_insert_outside_convex_hull(n,loc)
        elif loc_type==self.OUTSIDE_AFFINE_HULL:
            self.tri_insert_outside_affine_hull(n,loc)

        # for some of those actions, this could be skipped
        self.restore_delaunay(n)

    def tri_insert_in_face(self,n,loc):
        loc_f,loc_type,_ = loc
        a,b,c=self.cells['nodes'][loc_f]
        self.delete_cell(loc_f)
        self.add_edge(nodes=[n,a])
        self.add_edge(nodes=[n,b])
        self.add_edge(nodes=[n,c])
        self.add_cell(nodes=[n,a,b])
        self.add_cell(nodes=[n,b,c])
        self.add_cell(nodes=[n,c,a])
        
    def tri_insert_in_edge(self,n,loc):
        """ Takes care splitting the edge and any adjacent cells
        """
        loc_f,loc_type,loc_edge = loc 

        self.log.debug("Loc puts new vertex in edge %s"%loc_edge)
        cells_to_split=[]
        for c in self.edge_to_cells(loc_edge):
            if c<0: continue
            cells_to_split.append(  self.cells[c].copy() )
            self.log.debug("Deleting cell on insert %d"%c)
            self.delete_cell(c)

        # Modify the edge:
        a,c=self.edges['nodes'][loc_edge]
        b=n
        self.delete_edge(loc_edge)
        
        self.add_edge(nodes=[a,b])
        self.add_edge(nodes=[b,c])
        
        for cell_data in cells_to_split:
            common=[n for n in cell_data['nodes']
                    if n!=a and n!=c][0]
            jnew=self.add_edge(nodes=[b,common])
            
            for replace in [a,c]:
                nodes=list(cell_data['nodes'])
                idx=nodes.index(replace)
                nodes[idx]=b
                self.add_cell(nodes=nodes)
        
    def tri_insert_outside_convex_hull(self,n,loc):
        dim=self.dim()
        if dim==2:
            self.tri_insert_outside_convex_hull_2d(n,loc)
        elif dim==1:
            self.tri_insert_outside_convex_hull_1d(n,loc)
        else:
            assert False
    def tri_insert_outside_convex_hull_1d(self,n,loc):
        self.log.debug("tri_insert_outside_convex_hull_1d")
        n_adj=loc[2]
        self.add_edge(nodes=[n,n_adj])
    def tri_insert_outside_convex_hull_2d(self,n,loc):
        # HERE: 
        #   the CGAL code is a little funky because of the use of 
        #   infinite vertices and the like.
        #   the plan here:
        #   a. change 'locate' to return halfedges instead of just an
        #      edge.  otherwise we'd have to redo the orientation check here.
        #   b. traverse the half-edge forwards and backwards, accumulating
        #      lists of adjacent edges which also satisfy the CCW rule.
        #   c. create triangles with n and the given half-edge, as well as the
        #      accumulated adjacent edges
        #   the result then is that the convex hull is built out.
        # Triangulation_2.h:1132
        assert loc[0]==self.INF_CELL # sanity.
        he0=loc[2] # adjacent half-edge

        def check_halfedge(he):
            nodes=[he.node_rev(),he.node_fwd(),n]
            pnts=self.nodes['x'][nodes]
            ccw=robust_predicates.orientation(pnts[0],pnts[1],pnts[2])
            return ccw>0
        assert check_halfedge(he0)

        addl_fwd=[]
        he=he0.fwd()
        while check_halfedge(he):
            addl_fwd.append(he)
            he=he.fwd()
        addl_rev=[]
        he=he0.rev()
        while check_halfedge(he):
            addl_rev.append(he)
            he=he.rev()

        self.add_edge( nodes=[he0.node_rev(),n] )
        self.add_edge( nodes=[he0.node_fwd(),n] )
        self.add_cell( nodes=[he0.node_rev(),he0.node_fwd(),n] )
        for he in addl_fwd:
            self.add_edge( nodes=[he.node_fwd(),n] )
            # the second node *had* been ne0.node_fwd(), but that
            # was probably a typo.
            self.add_cell( nodes=[he.node_rev(),he.node_fwd(),n] )
        for he in addl_rev:
            self.add_edge( nodes=[he.node_rev(),n] )
            # same here.
            self.add_cell( nodes=[he.node_rev(),he.node_fwd(),n] )

        # 1. Check orientation.  Since we get an unoriented edge j_adj,
        #    all we can do is assert that the points are not collinear.
        # 2. loops through faces incident to infinite vertex (?)
        #    gathering a list of external edges which make a CCW triangle
        #    with the vertex to insert.  stop on the first edge which fails this.
        #    This is done first traversing CCW, then again traversing CW
        # 3. Make the new face with the given edge..
        # 

    def tri_insert_outside_affine_hull(self,n,loc):
        self.log.debug("Insert outside affine hull")

        loc_face,loc_type,curr_dim = loc

        if curr_dim==-1:
            self.log.debug("  no nodes, no work")
        elif curr_dim==0:
            self.log.debug("  simply add edge")
            for nbr in self.valid_node_iter():
                if nbr != n:
                    self.add_edge(nodes=[n,nbr])
        elif curr_dim==1:
            self.log.debug("  add edges and cells")
            # the strategy in Triangulation_2.h makes some confusing
            # use of the infinite face - take a less elegant, more explicit
            # approach here
            orig_edges=list(self.valid_edge_iter())
            for nbr in self.valid_node_iter():
                if nbr != n:
                    self.add_edge(nodes=[n,nbr])
            for j in orig_edges:
                n1,n2=self.edges['nodes'][j]
                self.add_cell( nodes=[n,n1,n2] )
        else:
            assert False
    def add_cell(self,_force_invariants=True,**kwargs):
        if _force_invariants:
            nodes=kwargs['nodes']

            # Make sure that topological invariants are maintained:
            # nodes are ordered ccw.
            # edges are populated
            # used to assume/force the edges to be sequenced opposite nodes.
            # but that is a triangulation-specific assumption, while we're using
            # a general unstructured_grid base class.  The base class makes
            # an incompatible assumption, that the first edge connects the first
            # two nodes.  
            pnts=self.nodes['x'][nodes]

            ccw=robust_predicates.orientation(pnts[0],pnts[1],pnts[2]) 
            assert ccw!=0
            if ccw<0:
                nodes=nodes[::-1]
                kwargs['nodes']=nodes
            
            j0=self.nodes_to_edge(nodes[0],nodes[1])
            j1=self.nodes_to_edge(nodes[1],nodes[2])
            j2=self.nodes_to_edge(nodes[2],nodes[0])
            kwargs['edges']=[j0,j1,j2]
        c=super(Triangulation,self).add_cell(**kwargs)

        # update the link from edges back to cells
        for ji,j in enumerate(self.cells['edges'][c]):
            # used to attempt to enforce this:
            #   ji-th edge is the (ji+1)%3,(ji+2)%3 nodes of the cell
            # but that's not compatible with checks in unstructured_grid
            # but need to know if the edge is in that order or the
            # opposite
            if self.edges['nodes'][j,0] == self.cells['nodes'][c,ji]:
                self.edges['cells'][j,0] = c
            else:
                self.edges['cells'][j,1] = c
        return c
                
    def flip_edge(self,j):
        """ 
        rotate the given edge CCW.  requires that triangular cells
        exist on both sides of the edge
        (that's not a hard and fast requirement, just makes it easier
        to implemenet. There *does* have to be a potential cell on either
        side).
        """
        c_left,c_right=self.edges['cells'][j,:]
        self.log.debug("Flipping edge %d, with cells %d, %d   nodes %d,%d"%(j,c_left,c_right,
                                                                            self.edges['nodes'][j,0],
                                                                            self.edges['nodes'][j,1]) )
        assert c_left>=0 # could be relaxed, at the cost of some complexity here
        assert c_right>=0
        # could work harder to preserve extra info:
        #c_left_data = self.cells[c_left].copy()
        #c_right_data = self.cells[c_right].copy()


        # This is dangerous! - deleting the cells means that topo_sort is no good,
        # and that breaks half-edge ops.
        # moving to happen a bit later -
        # self.delete_cell(c_left)
        # self.delete_cell(c_right)
        he_left=unstructured_grid.HalfEdge(self,j,0)
        he_right=unstructured_grid.HalfEdge(self,j,1)

        na,nc = self.edges['nodes'][j]
        nd=he_left.fwd().node_fwd()
        nb=he_right.fwd().node_fwd()

        # DBG
        if 0:
            for n,label in zip( [na,nb,nc,nd],
                                "abcd" ):
                plt.text( self.nodes['x'][n,0],
                          self.nodes['x'][n,1],
                          label)
        # keep the time where the cells are deleted to a minimum
        self.delete_cell(c_left)
        self.delete_cell(c_right)
                
        self.modify_edge(j,nodes=[nb,nd])
        new_left =self.add_cell(nodes=[na,nb,nd])
        new_right=self.add_cell(nodes=[nc,nd,nb])
        return new_left,new_right

    def delete_node(self,n):
        """ Triangulation version implies cascade, but also 
        patches up the triangulation
        """
        assert n>=0

        N=self.Nnodes_valid()
        if N==1:
            super(Triangulation,self).delete_node(n)
        elif N==2:
            j=self.node_to_edges(n)[0]
            self.delete_edge(j)
            super(Triangulation,self).delete_node(n)
        elif self.dim()==1:
            self.delete_node_1d(n)
        else:
            self.delete_node_2d(n)

    def delete_node_1d(self,n):
        # Triangulation_2.h hands this off to the triangulation data structure
        # That code looks like:
        assert self.dim() == 1
        assert self.Nnodes_valid() > 2


        # Two cases - either n is at the end of a line of nodes,
        # or it's between two nodes.
        nbrs=self.node_to_nodes(n)

        if len(nbrs)==1: # easy, we're at the end
            j=self.nodes_to_edge(n,nbrs[0])
            self.delete_edge(j)
            super(Triangulation,self).delete_node(n)
        else:
            assert len(nbrs)==2
            j1=self.nodes_to_edge(n,nbrs[0])
            j2=self.nodes_to_edge(n,nbrs[1])
            self.delete_edge(j1)
            self.delete_edge(j2)
            super(Triangulation,self).delete_node(n)
            self.add_edge( nodes=nbrs )

    def test_delete_node_dim_down(self,n):
        # see Triangulation_2.h : test_dim_down
        # test the dimensionality of the resulting triangulation
        # upon removing of vertex v
        # it goes down to 1 iff
        #  1) any finite face is incident to v
        #  2) all vertices are collinear
        assert self.dim() == 2
        for c in self.valid_cell_iter():
            if n not in self.cell_to_nodes(c):
                # There is a triangle not involving n
                # deleting n would retain a 2D triangulation
                return False
        pnts=[self.nodes['x'][i]
              for i in self.valid_node_iter()
              if i!=n]
        a,b = pnts[:2]
        for c in pnts[2:]:
            if robust_predicates.orientation(a,b,c) != 0:
                return False
        return True
        
    def delete_node_2d(self,n):
        if self.test_delete_node_dim_down(n):
            # deleting n yields a 1D triangulation - no faces
            for c in self.valid_cell_iter():
                self.delete_cell(c)
            # copy
            for j in list(self.node_to_edges(n)): 
                self.delete_edge(j)
            super(Triangulation,self).delete_node(n)
            return 

        # first, make a hole around n
        deletee=n

        # new way
        nbrs=self.angle_sort_adjacent_nodes(deletee)
        edges_to_delete=[]

        hole_nodes=[]
        for nbrA,nbrB in circular_pairs(nbrs):
            hole_nodes.append(nbrA)
            he=self.nodes_to_halfedge(nbrA,nbrB)
            if (he is None) or (he.cell()<0) or (n not in self.cell_to_nodes(he.cell())):
                hole_nodes.append('inf')
            edges_to_delete.append( self.nodes_to_edge( [deletee,nbrA] ) )

        for j in edges_to_delete:
            self.delete_edge_cascade(j)
        super(Triangulation,self).delete_node(deletee)

        # Use the boundary completion approach described in Devillers 2011
        # it's not terribly slow, and can be done with the existing
        # helpers.
        self.fill_hole(hole_nodes)
    def fill_hole(self,hole_nodes):
        
        # track potentially multiple holes
        # a few place use list-specific semantics - not ndarray
        hole_nodes=list(hole_nodes)
        holes_nodes=[ hole_nodes ]

        while len(holes_nodes):
            hole_nodes=holes_nodes.pop()

            while 'inf' in hole_nodes[:2]:
                hole_nodes = hole_nodes[1:] + hole_nodes[:1]
                
            a,b=hole_nodes[:2]

            self.log.debug("Considering edge %d-%d"%(a,b) )

            # inf nodes:
            # can't test any geometry.  seems like we can only have boundary
            # faces if the hole included an inf node.
            # so drop it from candidates here, but remember that we saw it

            # first, sweep through the candidates to test CCW
            has_inf=False
            c_cand1=hole_nodes[2:]
            c_cand2=[]
            for c in c_cand1:
                if c=='inf':
                    has_inf=True
                elif robust_predicates.orientation( self.nodes['x'][a],
                                                    self.nodes['x'][b],
                                                    self.nodes['x'][c] ) > 0:
                    c_cand2.append(c)

            self.log.debug("After CCW tests, %s are left"%c_cand2)

            while len(c_cand2)>1:
                c=c_cand2[0]
                for d in c_cand2[1:]:
                    tst=robust_predicates.incircle( self.nodes['x'][a],
                                                    self.nodes['x'][b],
                                                    self.nodes['x'][c],
                                                    self.nodes['x'][d] )
                    if tst>0:
                        self.log.debug("%d was inside %d-%d-%d"%(d,a,b,c))
                        c_cand2.pop(0)
                        break
                else:
                    # c passed all the tests
                    c_cand2=[c]
                    break
            # if the hole nodes are already all convex, then they already
            # form the new convex hull - n was on the hull and simply goes
            # away
            if has_inf and not c_cand2:
                c_cand2=['inf']
                c='inf' # was this missing??
            else:
                c=c_cand2[0]

            self.log.debug("Decided on %s-%s-%s"%(a,b,c))

            # n.b. add_cell_and_edges is probably what is responsible
            # for the painless dealing with collinear boundaries.
            if c!='inf':
                self.add_cell_and_edges( nodes=[a,b,c] )

            # what hole to put back on the queue?
            if len(hole_nodes)==3:
                # finished this hole.
                self.log.debug("Hole is finished")
                continue
            elif c==hole_nodes[2]:
                self.log.debug("Hole is trimmed from front")
                hole_nodes[:3] = [a,c]
                holes_nodes.append( hole_nodes )
            elif c==hole_nodes[-1]:
                self.log.debug("Hole is trimmed from back")
                hole_nodes=hole_nodes[1:] # drop a
                self.log.debug("  New hole is %s"%hole_nodes)
                holes_nodes.append( hole_nodes )
            else:
                self.log.debug("Created two new holes")
                idx=hole_nodes.index(c)

                h1=hole_nodes[1:idx+1]
                h2=hole_nodes[idx:] + hole_nodes[:1]
                self.log.debug("  New hole: %s"%h1)
                self.log.debug("  New hole: %s"%h2)

                holes_nodes.append( h1 )
                holes_nodes.append( h2 )


    # Make a check for the delaunay criterion:
    def check_global_delaunay(self):
        bad_checks=[] # [ (cell,node),...]
        for c in self.valid_cell_iter():
            nodes=self.cells['nodes'][c]
            pnts=self.nodes['x'][nodes]

            # brute force - check them all.
            for n in self.valid_node_iter():
                if n in nodes:
                    continue
                t=self.nodes['x'][n]
                check=robust_predicates.incircle(pnts[0],pnts[1],pnts[2],t)
                if check>0:
                    # how do we check for constraints here?
                    # maybe more edge-centric?
                    # tests of a cell on one side of an edge against a node on the
                    # other is reflexive.
                    # 
                    
                    # could go through the edges of c, 
                    msg="Node %d is inside the circumcircle of cell %d (%d,%d,%d)"%(n,c,
                                                                                    nodes[0],nodes[1],nodes[2])
                    self.log.error(msg)
                    bad_checks.append( (c,n) )
        return bad_checks
    
    def check_local_delaunay(self):
        """ Check both sides of each edge - can deal with constrained edges.
        """
        bad_checks=[] # [ (cell,node),...]
        for j in self.valid_edge_iter():
            if self.edges['constrained'][j]:
                continue
            c1,c2 = self.edge_to_cells(j)
            if c1<0 or c2<0:
                continue
            # always check the smaller index -
            # might help with caching later on.
            c=min(c1,c2)
            c_opp=max(c1,c2)
            
            nodes=self.cells['nodes'][c]
            pnts=self.nodes['x'][nodes]

            # brute force - check them all.
            for n in self.cell_to_nodes(c_opp):
                if n in nodes:
                    continue
                t=self.nodes['x'][n]
                check=robust_predicates.incircle(pnts[0],pnts[1],pnts[2],t)
                if check>0:
                    msg="Node %d is inside the circumcircle of cell %d (%d,%d,%d)"%(n,c,
                                                                                    nodes[0],nodes[1],nodes[2])
                    self.log.error(msg)
                    bad_checks.append( (c,n) )
                    raise Exception('fail')
        return bad_checks
    
    def restore_delaunay(self,n):
        """ n: node that was just inserted and may have adjacent cells
        which do not meet the Delaunay criterion
        """
        # n is node for Vertex_handle v
        if self.dim() <= 1:
            return

        # a vertex is shared by faces, but "stores" only one face.
        # Face_handle f=v->face();

        # This code iterates over the faces adjacent to v
        # in ccw order.

        # Face_handle next;
        # int i;
        # Face_handle start(f);
        # do {
        #   i = f->index(v);
        #   next = f->neighbor(ccw(i));  // turn ccw around v
        #   propagating_flip(f,i);
        #   f=next;
        # } while(next != start);

        # Shaky on the details, but for starters, try marking the CCW sweep
        # based on neighbor nodes.
        nbr_nodes=self.angle_sort_adjacent_nodes(n)
        
        start=trav=nbr_nodes[0]
        trav_next=nbr_nodes[1]

        while 1:
            c=self.nodes_to_cell( [n,trav,trav_next],fail_hard=False)
            if c is not None:    
                for i in [0,1,2]:
                    if self.cells['nodes'][c,i]==n:
                        break
                else:
                    assert False
            # line up the next cell before modifying this one
            nbr_nodes=self.angle_sort_adjacent_nodes(n,ref_nbr=trav_next)
            trav=trav_next
            trav_next=nbr_nodes[1]

            if c is not None:
                self.propagating_flip(c,i)
                
            if trav == start:
                break

        if self.post_check:
            bad=self.check_local_delaunay()
            if bad:
                raise self.GridException("Delaunay criterion violated")

    def propagating_flip(self,c,i):
        # this is taken from non_recursive_propagating_flip
        # c: cell, akin to face_handle
        # i: index of the originating vertex in cell c.

        # track the stack based on the halfedge one place CW
        # from the edge to be flipped.

        edges=[] # std::stack<Edge> edges;
        vp = self.cells['nodes'][c,i]  #  const Vertex_handle& vp = f->vertex(i);
        p=self.nodes['x'][vp] # const Point& p = vp->point();

        # maybe better to use half-edges here.
        # ordering of edges is slightly different than CGAL.
        # if i gives the vertex, 
        # edges.push(Edge(f,i)); # this is the edge *opposite* vp
        # for our ordering, need edge i+1
        edges.append( self.cell_to_halfedge(c,i) )

        while edges: # (! edges.empty()){
            #const Edge& e = edges.top()
            he=edges[-1]

            he_flip=he.fwd()
            # not sure about this part:
            if self.edges['constrained'][he_flip.j]:
                edges.pop()
                continue
            
            nbr=he_flip.cell_opp()

            if nbr>=0:
                # assuming that ON_POSITIVE_SIDE would mean that p (the location of the
                # originating vertex) is *inside* the CCW-defined circle of the neighbor
                # and would thus mean that the delaunay criterion is not satisfied.
                #if ON_POSITIVE_SIDE != side_of_oriented_circle(n,  p, true):
                nbr_points= self.nodes['x'][ self.cells['nodes'][nbr] ]

                p_in_nbr = robust_predicates.incircle(nbr_points[0],
                                                      nbr_points[1],
                                                      nbr_points[2],
                                                      p )
                #if side_of_oriented_circle(n,  p, true) == ON_POSITIVE_SIDE:
                if p_in_nbr > 0: 
                    self.flip_edge(he_flip.j)
                    extra=he.rev().opposite()
                    edges.append(extra)
                    continue
            edges.pop() # drops last item
            continue

    def find_intersected_elements(self,nA,nB):
        """ 
        returns a history of the elements traversed.
        this includes:
          ('node',<node index>)
          ('edge',<half edge>)
          ('cell',<cell index>)

        note that traversing along an edge is not included - but any
        pair of nodes in sequence implies an edge between them.
        """
        assert nA!=nB
        assert not self.nodes['deleted'][nA]
        assert not self.nodes['deleted'][nB]

        def ordered(x1,x2,x3):
            # given collinear points, return true if they are in order
            # along that line
            if x1[0]!=x2[0]:
                i=0
            else:
                i=1
            return (x1[i]<x2[i]) == (x2[i]<x3[i])
        
        # traversal could encounter multiple types of elements
        trav=('node',nA)
        A=self.nodes['x'][nA]
        B=self.nodes['x'][nB]

        history=[trav]

        if self.dim()==1:
            assert trav[0]=='node'
            n_nbrs=self.node_to_nodes(trav[1])
            for n_nbr in n_nbrs:
                if n_nbr==nB:
                    history.append( ('node',nB) )
                    return history
                if ordered( self.nodes['x'][nA],
                            self.nodes['x'][n_nbr],
                            self.nodes['x'][nB] ):
                    trav=('node',n_nbr)
                    history.append( trav )
                    he=self.nodes_to_halfedge(nA,n_nbr)
                    break
            else:
                assert False # should never get here
            
            while trav!=('node',nB):
                he=he.fwd()
                trav=('node',he.node_fwd())
                history.append(trav)
            return history
        else:
            while trav!=('node',nB):
                if trav[0]=='node':
                    ntrav=trav[1]
                    for c in self.node_to_cells(ntrav):
                        cn=self.cell_to_nodes(c)
                        # print "At node %d, checking cell %d (%s)"%(ntrav,c,cn)
                        ci_trav=list(cn).index(ntrav) # index of ntrav in cell c
                        nD=cn[(ci_trav+1)%3]
                        nE=cn[(ci_trav+2)%3]
                        if nD==nB or nE==nB:
                            trav=('node',nB)
                            # print "Done"
                            break

                        D=self.nodes['x'][nD]
                        oD=robust_predicates.orientation( A,B,D )
                        if oD>0:
                            continue
                        N=self.nodes['x'][ntrav]
                        if oD==0 and ordered(N,D,B):
                            # fell exactly on the A-B segment, and is in the
                            # right direction
                            trav=('node',nD)
                            break

                        E=self.nodes['x'][nE]
                        oE=robust_predicates.orientation( A,B,E )
                        if oE<0:
                            continue
                        if oE==0 and ordered(N,E,B):
                            # direction
                            trav=('node',nE)
                            break
                        j=self.cell_to_edges(c)[ (ci_trav+1)%3 ]
                        j_nbrs=self.edge_to_cells(j)
                        # AB crosses an edge - record the edge, and the side we are
                        # approaching from:
                        history.append( ('cell',c) )
                        if j_nbrs[0]==c:
                            trav=('edge',self.halfedge(j,0))
                            # making sure I got the 0/1 correct
                            assert trav[1].cell()==c
                            break
                        elif j_nbrs[1]==c:
                            trav=('edge',self.halfedge(j,1))
                            # ditto
                            assert trav[1].cell()==c
                            break
                        assert False
                elif trav[0]=='edge':
                    he=trav[1].opposite()
                    #jnodes=self.edges['nodes'][j]
                    # have to choose between the opposite two edges or their common
                    # node:
                    c_next=he.cell()
                    history.append( ('cell',c_next) )

                    nD=he.fwd().node_fwd()
                    # print "Entering cell %d with nodes %s"%(c_next,self.cell_to_nodes(c_next))

                    oD=robust_predicates.orientation( A,B, self.nodes['x'][nD] )
                    if oD==0:
                        trav=('node',nD)
                    elif oD>0:
                        # going to cross
                        trav=('edge',he.fwd())
                    else:
                        trav=('edge',he.rev())
                else:
                    assert False
                history.append(trav)
        return history

    def add_constraint(self,nA,nB):
        jAB=self.nodes_to_edge([nA,nB])
        if jAB is not None:
            # no work to do - topology already good.
            assert not self.edges['constrained'][jAB]
            self.edges['constrained'][jAB]=True
            return

        # inserting an edge from 0-5.
        int_elts=self.find_intersected_elements(nA,nB)

        # Now we need to record the two holes bordered the new edge:
        left_nodes=[nA] # will be recorded CW
        right_nodes=[nA] # will be recorded CCW

        # Iterate over the crossed elements, checking that the new
        # edge doesn't encounter any collinear nodes or other constrained
        # edges.  Build up the nodes of the holes at the same time.
        dead_cells=[]
        dead_edges=[]
        for elt in int_elts[1:-1]:
            if elt[0]=='node':
                raise self.ConstraintCollinearNode("Constraint intersects a node")
            if elt[0]=='cell':
                dead_cells.append(elt[1])
            if elt[0]=='edge':
                if self.edges['constrained'][ elt[1].j ]:
                    raise IntersectingConstraints("Constraint intersects a constraint")
                next_left=elt[1].node_fwd()
                if left_nodes[-1]!=next_left:
                    left_nodes.append(next_left)
                next_right= elt[1].node_rev()
                if right_nodes[-1]!=next_right:
                    right_nodes.append(next_right)
                dead_edges.append(elt[1].j)
        left_nodes.append(nB)
        right_nodes.append(nB)
        left_nodes = left_nodes[::-1]

        # tricky business here
        # but the delaunay business is only invoked on node operations - leaving
        # the edge/cell operations free and clear to violate invariants
        for c in dead_cells:
            self.delete_cell(c)
        for j in dead_edges:
            self.delete_edge(j)

        self.add_edge(nodes=[nA,nB],constrained=True)
            
        # and then sew up the holes!
        self.fill_hole( left_nodes )
        self.fill_hole( right_nodes )

    def remove_constraint(self,nA,nB):
        j=self.nodes_to_edge([nA,nB])
        assert self.edges['constrained'][j]
        self.edges['constrained'][j]=False

        c1,c2=self.edge_to_cells(j)
        if (c1>=0) and (c2>=0):
            c=c1 # can we just propagate from one side?
            for ni,n in enumerate(self.cell_to_nodes(c1)):
                if n not in self.edges['nodes'][j]:
                    self.propagating_flip(c1,ni)
                    break
        if self.post_check:
            self.check_local_delaunay()

    def bulk_init_slow(self,points):
        raise Exception("No - it's really slow.  Don't do this.")
    
    def bulk_init(self,points): # ExactDelaunay
        if spatial is None:
            return self.bulk_init_slow(points)
        
        sdt = spatial.Delaunay(points)

        self.nodes=np.zeros( len(points), self.node_dtype)
        self.cells=np.zeros( sdt.vertices.shape[0], self.cell_dtype)

        self.nodes['x']=points
        self.cells['nodes']=sdt.vertices

        # looks like it's CGAL style:
        # neighbor[1] shares nodes[0] and nodes[2]
        # vertices are CCW

        for c in range(self.Ncells()):
            for i,(a,b) in enumerate(circular_pairs(self.cells['nodes'][c])):
                # first time - that would be i=0, and the first two nodes.
                # but for neighbors, it's indexed by the opposite node.  so the edge
                # connected the nodes[0]--nodes[1] corresponds with neighbor 2.
                c_nbr=sdt.neighbors[c,(i+2)%3]

                # c_nbr==-1 on convex hull.
                # only worry about cases where c is larger.
                if c<c_nbr:
                    continue

                if c_nbr<0:
                    c_nbr=self.INF_CELL

                j=self.add_edge(nodes=[a,b],
                                cells=[c,c_nbr])
                # and record in the cell, too
                self.cells['edges'][c,i]=j
                if c_nbr!=self.INF_CELL:
                    nbr_nodes=self.cells['nodes'][c_nbr]
                    for i_nbr in [0,1,2]:
                        if nbr_nodes[i_nbr]==b and nbr_nodes[(i_nbr+1)%3]==a:
                            self.cells['edges'][c_nbr,i_nbr]=j
                            break
                    else:
                        assert False

            
# Issues:
#   Calls like edge_to_cells do not scale well right now.  In particular,
#   it would be better in this code to always specify the edge, so that 
#   a full scan isn't necessary.
