"""Measured-grid search. Reachable means a sampled error meets tolerance;
it never asserts that unsampled space is physically unreachable."""
import numpy as np
from scipy.spatial import cKDTree, QhullError
from scipy.interpolate import LinearNDInterpolator
from scipy.optimize import minimize
from .interp import TunerResponseModel
from .rf import RFNetworkEngine as RF
from .models import SearchResult

class GammaSearchEngine:
    def __init__(self,data,z0=50.,termination=0j,fixture=None):
        self.data=data; self.z0=z0; self.termination=termination; self.fixture=fixture
        self.frequencies=np.asarray(data['frequencies'],float)
        if self.frequencies.ndim!=1 or len(self.frequencies)==0 or np.any(np.diff(self.frequencies)<=0): raise ValueError('frequencies must increase')
        self.indices=np.flatnonzero(np.asarray(data['valid'],bool))
        if not len(self.indices): raise ValueError('no valid calibration points')
        actual=np.asarray(data.get('actual',data['positions']),float)
        self.positions=actual[self.indices]
        self.s=np.asarray(data['s'])[self.indices]
        if self.s.shape!=(len(self.indices),len(self.frequencies),2,2) or not np.isfinite(self.s).all() or not np.isfinite(self.positions).all(): raise ValueError('invalid calibration arrays')
        self._cache={}
    def _at_frequency(self,array,f):
        if not np.isfinite(f) or not self.frequencies[0]<=f<=self.frequencies[-1]: raise ValueError('frequency outside calibration band')
        j=int(np.searchsorted(self.frequencies,f))
        if j<len(self.frequencies) and self.frequencies[j]==f: return array[...,j,:,:]
        t=(f-self.frequencies[j-1])/(self.frequencies[j]-self.frequencies[j-1])
        return array[...,j-1,:,:]*(1-t)+array[...,j,:,:]*t
    def gammas(self,frequency):
        f=float(frequency)
        if f not in self._cache:
            s=self._at_frequency(self.s,f)
            fixture=self.fixture
            if fixture is not None:
                fixture=np.asarray(fixture)
                if fixture.shape!=(2,2): fixture=self._at_frequency(fixture,f)
            g=np.asarray(RF.dut_gamma(s,self.termination,fixture),complex)
            if not np.isfinite(g).all(): raise ValueError('nonfinite load reflection')
            self._cache[f]=(g,cKDTree(np.column_stack((g.real,g.imag))))
        return self._cache[f][0]
    def _result(self,local,g,error,tolerance):
        return SearchResult(int(self.indices[local]),self.positions[local].copy(),complex(g),RF.gamma_to_z(g,self.z0),float(error),bool(error<=tolerance))
    def search(self,frequency,target_gamma,k=5,tolerance=.05):
        target=complex(target_gamma)
        if not np.isfinite(target) or k<1 or tolerance<0: raise ValueError('invalid search arguments')
        g=self.gammas(frequency); tree=self._cache[float(frequency)][1]
        distances,indices=tree.query([target.real,target.imag],k=min(int(k),len(g)))
        return [self._result(int(i),g[i],e,tolerance) for e,i in zip(np.atleast_1d(distances),np.atleast_1d(indices))]
    def search_multi(self,frequencies,targets,weights=None,k=5,tolerance=.05):
        fs=np.asarray(frequencies,float); ts=np.asarray(targets,complex)
        if fs.ndim!=1 or fs.size==0 or ts.shape!=fs.shape or not np.isfinite(ts).all() or k<1: raise ValueError('invalid targets')
        w=np.ones(len(fs)) if weights is None else np.asarray(weights,float)
        if w.shape!=fs.shape or not np.isfinite(w).all() or np.any(w<0) or w.sum()<=0: raise ValueError('invalid weights')
        gs=np.array([self.gammas(f) for f in fs]); errors=np.sqrt(np.average(abs(gs-ts[:,None])**2,axis=0,weights=w))
        return [self._result(i,gs[0,i],errors[i],tolerance) for i in np.argsort(errors)[:k]]
    def optimize_local(self,frequency,target_gamma):
        """Convex-hull linear interpolation; result is prediction, never measured.
        Fall back explicitly when geometry is degenerate or optimization fails.
        """
        seed=self.search(frequency,target_gamma,k=1)[0]; g=self.gammas(frequency)
        scale=np.ptp(self.positions,axis=0); active=scale>1e-12
        if not active.any(): return {'position':seed.position,'gamma':seed.gamma,'error':seed.error,'predicted':True,'interpolated':False}
        p=self.positions[:,active]; lo=p.min(axis=0); scale=scale[active]; p=(p-lo)/scale
        try:
            if p.shape[1]==1:
                order=np.argsort(p[:,0]); xp=p[order,0]; vals=g[order]
                def interp(x): return np.interp(x[0],xp,vals.real)+1j*np.interp(x[0],xp,vals.imag)
            else:
                fn=LinearNDInterpolator(p,g)
                def interp(x): return complex(np.asarray(fn(x)).item())
            def objective(x):
                v=interp(x)
                return abs(v-target_gamma)**2 if np.isfinite(v) else 1e6
            start=(seed.position[active]-lo)/scale
            opt=minimize(objective,start,method='SLSQP',bounds=[(0,1)]*len(start),options={'maxiter':100,'ftol':1e-12})
            value=interp(opt.x)
            if not opt.success or not np.isfinite(value) or abs(value-target_gamma)>seed.error: raise ValueError('no improvement')
            pos=seed.position.copy(); pos[active]=lo+opt.x*scale
            return {'position':pos,'gamma':value,'error':float(abs(value-target_gamma)),'predicted':True,'interpolated':True}
        except (QhullError,ValueError):
            return {'position':seed.position,'gamma':seed.gamma,'error':seed.error,'predicted':True,'interpolated':False}

    # -- interpolated tuning ---------------------------------------------
    def response_model(self, level_policy: str = 'closest') -> TunerResponseModel:
        """The interpolated response model of the calibrated tuner.

        The model interpolates the four S-parameters — not the reflection
        coefficient — exactly as the standard method describes, and the termination and the
        fixture are applied afterwards.  On a complete tensor grid this is their
        second-order Lagrange scheme; on scattered points it is a local
        second-order polynomial fit.
        """
        return TunerResponseModel(self.positions, self.s, self.frequencies,
                                  self.termination, self.fixture,
                                  level_policy=level_policy)

    def tune(self, frequency, target_gamma, method: str = 'two-stage',
             limits=None, level_policy: str = 'closest', grid_levels: int = 5,
             grid_span: float = 0.05) -> dict:
        """Tune to a target reflection coefficient with the interpolated model.

        ``method='general'`` stops after the general search (the closest
        calibrated point), ``'gradient'`` adds the local search on the
        interpolated response, and ``'two-stage'`` (default) runs both and then
        refines on a fine grid.  Every stage reports its own
        error, so the benefit of each step is visible.
        """
        model = self.response_model(level_policy)
        return model.tune(target_gamma, frequency, limits=limits, method=method,
                          grid_levels=grid_levels, grid_span=grid_span)
