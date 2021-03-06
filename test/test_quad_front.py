from stompy.grid import orthogonalize, front, unstructured_grid, exact_delaunay

from stompy.model.delft import dfm_grid
from stompy import utils

import matplotlib.pyplot as plt

try:
    reload
except NameError:
    from importlib import reload

## 
reload(unstructured_grid)
reload(exact_delaunay)
reload(dfm_grid)

#- # 

g=dfm_grid.DFMGrid("data/lsb_combined_v14_net.nc")

zoom=(578260, 579037, 4143970., 4144573)
point_in_poly=(578895, 4144375)
point_on_edge=(579003., 4144517.)
j_init=g.select_edges_nearest(point_on_edge)

dim_par=10.0 # 10m wide for edges "parallel" to j
dim_perp=20.0 # 20m long for edges "perpendicular" to j.

g.edge_to_cells()

#  plt.figure(1).clf()
#  
#  fig,ax=plt.subplots(num=1)
#  
#  g.plot_edges(ax=ax,clip=zoom)
#  
#  ax.text(point_in_poly[0],point_in_poly[1],'START')
#  
#  g.plot_edges(mask=[j_init],color='r',lw=2)
#  
#  if 0:
#      coll=g.plot_halfedges(values=af.grid.edges['cells'],clip=zoom,
#                            ax=ax)
#      coll.set_clim([-3,0])
#      coll.set_lw(0)
#  
#  # The idea is to start with an edge adjacent to an unfilled
#  # area (and with an existing cell on the other side).
#  
#  # This is the starting point for paving
#  # paving will only use rectangles.
#  
#  
#  ## 
#  af.plot_summary(label_nodes=False,clip=zoom)
#  ax.axis(zoom)

## 

reload(front)

af=front.AdvancingQuads(grid=g.copy(),scale=dim_par,perp_scale=dim_perp)
af.grid.edges['para']=0 # avoid issues during dev

af.add_existing_curve_surrounding(point_in_poly)
af.orient_quad_edge(j_init,af.PARA)

## 

af.loop()

## 

plt.figure(1).clf()
fig,ax=plt.subplots(num=1)

af.grid.plot_edges(ax=ax,clip=zoom)
af.grid.plot_edges(mask=[j_init],color='r',lw=2)

if 0:
    af.plot_summary(label_nodes=False,clip=zoom)

af.grid.plot_cells(centers=True,clip=zoom)

ax.axis(zoom)

## 
# Problem 1: going around a bend it starts making trapezoids, 
#   never really recovers.  A more nuanced cost function with angles
#   would help here.

# Enhancement: there is an obvious place to put a triangle around a bend.
#   will get there eventually.

# other stopping strategies (triangle?) may become necessary..


## 

# thoughts on how to optimize out the trapezoids:
#  - more weight on the lengths could help, but it's nuanced since 
#    some of the edges can only be made longer by creating a trapezoid.
#  - 
