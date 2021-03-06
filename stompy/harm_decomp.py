# harmonic decomposition

from safe_pylab import *
from numpy import *
from numpy.linalg import norm,qr,pinv
import tide_consts    


###
def recompose(t,comps,omegas):
    d = zeros(t.shape,float64)
    
    for i in range(len(omegas)):
        d += comps[i,0] * cos(t*omegas[i] - comps[i,1])
    return d
            
def decompose(t,h,omegas):
    """ take an arbitrary timeseries defined by times t and values h plus a list
    of N frequencies omegas, which must be ANGULAR frequencies (don't forget the 2pi)
    
    return comps as an Nx2 array, where comps[:,0] are the amplitudes and comps[:,1]
    are the phases.

    super cheap caching: remembers the last t and omegas, and if they are the same
    it will reuse the matrix from before.
    """
    def sim(a,b):
        if a is b:
            return True
        if a is None or b is None:
            return False
        return (a.shape == b.shape) and allclose(a,b)
    if sim(decompose.cached_t,t) and sim(decompose.cached_omegas,omegas):
        Ainv = decompose.cached_Ainv
    else:
        # A is a matrix of basis functions - two (cos/sin) for each frequency
        n_bases = 2*len(omegas)
        basis_len = len(h)

        # form the linear system
        # each column of A is a basis function
        A = zeros( (basis_len,n_bases), float64)

        for i in range(len(omegas)):
            A[:,2*i] = cos(omegas[i]*t)
            A[:,2*i+1] = sin(omegas[i]*t)

        Ainv = pinv(A)

        decompose.cached_Ainv=Ainv
        decompose.cached_t = t.copy()
        decompose.cached_omegas = omegas.copy()
        
        # and can we say anything about the conditioning of A ?
        def cond_num(L):
            return norm(L,ord=2)*norm(pinv(L),ord=2)

        # sort of arbitrary...
        cnum = cond_num(A)
        if cnum > 10:
            print "Harmonic decomposition: condition number may be too high: ",cnum
        
    x=dot(Ainv,h)

    # now rows are constituents, and we get the cos/sin as two columns
    comps = reshape(x,(len(omegas),2))

    # now transform cos/sin into amp/phase
    x_amps = sqrt( comps[:,0]**2 + comps[:,1]**2 )

    #
    x_phis = arctan2( comps[:,1], comps[:,0] )

    # rewrite comps using the amp/phase
    comps[:,0] = x_amps
    comps[:,1] = x_phis

    if 0:
        # check to see how close we are:
        recomposed = recompose(t,comps,omegas)
        rms = sqrt( ((h - recomposed)**2).mean() )

        print "RMS Error:",rms
    
    return comps

def noaa_37_names():
    """ return names of the 37 constituents provided in NOAA harmonic data
    """
    return ["M2","S2","N2","K1","M4","O1","M6","MK3","S4","MN4","NU2",
            "S6","MU2","2N2","OO1","LAM2","S1","M1","J1","MM","SSA",
            "SA","MSF","MF","RHO","Q1","T2","R2","2Q1","P1","2SM2",
            "M3","L2","2MK3","K2","M8","MS4"]

def noaa_37_omegas():
    """
    return frequencies in inverse seconds for the 37 NOAA constituents
    """
    idx = [tide_consts.const_names.index(n) for n in noaa_37_names()]
    omega_deg_per_hour = tide_consts.speeds[idx]
    omega_per_sec = omega_deg_per_hour * (1./3600) * (1/360.)
    return omega_per_sec
    

def decompose_noaa37(t,h):
    return decompose(t,h,noaa_37_omegas())


decompose.cached_t = None
decompose.cached_omegas = None

if __name__ == '__main__':
    # A sample problem:
    omegas = array([1.0,0.0])

    # the constructed data:
    amps = array([1,5.0])
    phis = array([1,0])

    t = linspace(0,10*pi,125)
    h = amps[0]*cos(omegas[0]*t - phis[0]) + amps[1]*cos(omegas[1]*t - phis[1])

    plot(t,h)

    comps = decompose(t,h,omegas)

    print "Components: ",comps

