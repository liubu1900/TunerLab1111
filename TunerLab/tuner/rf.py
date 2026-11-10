"""50-ohm power-wave networks with real, common reference impedance.
Port 1 faces DUT; port 2 faces termination. Fixture port 1 faces DUT.
"""
import numpy as np

class RFNetworkEngine:
    @staticmethod
    def z_to_gamma(z, z0=50.):
        if not np.isfinite(z0) or z0 <= 0: raise ValueError('z0 must be positive real')
        a=np.asarray(z,dtype=complex)
        if np.any(np.isclose(a+z0,0)): raise ValueError('impedance maps to a pole')
        with np.errstate(invalid='ignore', divide='ignore'): g=(a-z0)/(a+z0)
        g=np.where(np.isinf(np.abs(a)),1+0j,g)
        return complex(g) if g.ndim==0 else g
    @staticmethod
    def gamma_to_z(gamma,z0=50.):
        if not np.isfinite(z0) or z0 <= 0: raise ValueError('z0 must be positive real')
        g=np.asarray(gamma,dtype=complex)
        with np.errstate(invalid='ignore', divide='ignore'): z=z0*(1+g)/(1-g)
        z=np.where(g==1,complex(np.inf),z)
        return complex(z) if z.ndim==0 else z
    @staticmethod
    def terminated_gamma(s, termination=0j):
        a=np.asarray(s,dtype=complex)
        if a.shape[-2:] != (2,2): raise ValueError('S must end in 2x2')
        d=1-a[...,1,1]*termination
        if np.any(np.abs(d)<1e-12): raise ValueError('singular terminated network')
        return a[...,0,0]+a[...,0,1]*a[...,1,0]*termination/d
    @staticmethod
    def cascade(first,second):
        """Redheffer cascade, no S21 division, supports zero transmission."""
        a,b=np.broadcast_arrays(np.asarray(first,complex),np.asarray(second,complex))
        if a.shape[-2:] != (2,2): raise ValueError('S must end in 2x2')
        d=1-a[...,1,1]*b[...,0,0]
        if np.any(np.abs(d)<1e-12): raise ValueError('singular cascade')
        c=np.empty_like(a)
        c[...,0,0]=a[...,0,0]+a[...,0,1]*b[...,0,0]*a[...,1,0]/d
        c[...,0,1]=a[...,0,1]*b[...,0,1]/d
        c[...,1,0]=b[...,1,0]*a[...,1,0]/d
        c[...,1,1]=b[...,1,1]+b[...,1,0]*a[...,1,1]*b[...,0,1]/d
        return c
    @staticmethod
    def dut_gamma(tuner_s, termination=0j, fixture=None):
        g=RFNetworkEngine.terminated_gamma(tuner_s,termination)
        return g if fixture is None else RFNetworkEngine.terminated_gamma(fixture,g)
    @staticmethod
    def cascade_deembed(fixture, measured):
        """Undo :meth:`cascade`: given the fixture and c = cascade(fixture, S),
        return the original network S (push the reference plane back through the
        fixture).  Solves the Redheffer algebra for the *second* network directly,
        which is what de-embedding needs.  ``fixture`` is a single 2x2 S-matrix.
        """
        a=np.asarray(fixture,complex); c=np.asarray(measured,complex)
        if a.shape != (2,2) or c.shape != (2,2):
            raise ValueError('S must be 2x2')
        f11,f12,f21,f22 = a[0,0],a[0,1],a[1,0],a[1,1]
        c00,c01,c10,c11 = c[0,0],c[0,1],c[1,0],c[1,1]
        if abs(f12*f21) < 1e-15:
            raise ValueError('fixture off-diagonal terms are singular')
        denom = 1.0 + f22*(c00 - f11)/(f12*f21)
        with np.errstate(divide='ignore', invalid='ignore'):
            d = 1.0/denom
        s11 = (c00 - f11)*d/(f12*f21)
        s12 = c01*d/f12
        s21 = c10*d/f21
        s22 = c11 - s21*f22*s12/d
        return np.array([[s11, s12], [s21, s22]], dtype=complex)

z_to_gamma=RFNetworkEngine.z_to_gamma
gamma_to_z=RFNetworkEngine.gamma_to_z
