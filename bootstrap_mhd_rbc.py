"""
Dedalus script for 3D mhd Rayleigh-Benard convection.

This script uses a Fourier basis in the horizontal direction(s) with periodic boundary
conditions. The vertical direction is represented as Chebyshev coefficients.
The equations are scaled in units of the viscous diffusion time.

By default, the boundary conditions are:
    Velocity: Impenetrable, no-slip at both the top and bottom
    Magnetic: Conducting (top & bot)
    Thermal:  Fixed temp (top & bot)

Usage:
    bootstrap_mhd_rbc.py [options]
    bootstrap_mhd_rbc.py <config> [options]

Options:
    --Ra=<Rayleigh>            The Rayleigh number [default: 1e5]
    --Pr=<Prandtl>             The Prandtl number [default: 1]
    --Q=<Chandra>              The Chandrasehkar number [default: 1]
    --Pm=<MagneticPrandtl>     The Magnetic Prandtl number [default: 1]
    --a=<aspect>               Aspect ratio of problem [default: 2]
    --3D                       changes to 3D (default: 2.5D)

    --nz=<nz>                  Vertical resolution [default: 128]
    --nx=<nx>                  Horizontal resolution [default: 256]
    --ny=<nx>                  Horizontal resolution [default: 256]

    --FF                       Fixed flux boundary conditions top/bottom (FF)
    --FT                       Fixed flux boundary conditions at bottom fixed temp at top (TT)
    --FS                       Free-slip boundary conditions top/bottom (default: no-slip)
    --MI                       Use electrically insulating bc (default: conducting)

    --mesh=<mesh>              Processor mesh if distributing 3D run in 2D 
    
    --run_time_wall=<time>     Run time, in hours [default: 23.5]

    --restart=<file>           Restart from checkpoint file
    --overwrite                If flagged, force file mode to overwrite
    --seed=<seed>              RNG seed for initial conditoins [default: 42]

    --label=<label>            Optional additional case name label
    --verbose                  Do verbose output (e.g., sparsity patterns of arrays)
    --no_join                  If flagged, don't join files at end of run
    --root_dir=<dir>           Root directory for output [default: ./]
    --safety=<s>               CFL safety factor [default: 0.7]

    --noise_modes=<N>          Number of wavenumbers to use in creating noise; for resolution testing
  
    --alp=<power>          The power of Ra for the path [default: 1]
    --β=<power>          The power of Q for the path [default: 0]
    --logStep=<step>     The size of step to take, in log space, while bootstrapping. 
                         Take Ra_F step of this size if α != 0, otherwise Q. [default: 1/4]
    --Nboots=<N>     Max number of bootstrap steps to take [default: 12]
    --boot_time=<t>      Minimum time to spend on each bootstrap step, in buoyancy times. [default: 100]
    --SBDF2              Uses SBDF2 timestepper
    --SBDF4              Uses SBDF4 timestepper
    --factor=<f>         Factor for timestepping
"""
import logging
import os
import sys
import time
from configparser import ConfigParser
from pathlib import Path
from fractions import Fraction

import numpy as np
from docopt import docopt
from mpi4py import MPI
from pandas import DataFrame

from dedalus import public as de
from dedalus.extras import flow_tools
from dedalus.tools  import post
from dedalus.tools.config import config

from logic.output import initialize_magnetic_output
from logic.checkpointing import Checkpoint
from logic.ae_tools import BoussinesqAESolver
from logic.extras import global_noise
from logic.parsing import construct_BC_dict, construct_out_dir

logger = logging.getLogger(__name__)

args   = docopt(__doc__)
if args['<config>'] is not None: 
    config_file = Path(args['<config>'])
    config = ConfigParser()
    config.read(str(config_file))
    for n, v in config.items('parameters'):
        for k in args.keys():
            if k.split('--')[-1].lower() == n:
                if v == 'true': v = True
                args[k] = v

### 1. Read in command-line args, set up data directory
threeD = args['--3D']
bc_dict = construct_BC_dict(args, default_T_BC='TT', default_u_BC='NS', default_M_BC='MC')
if args['--MI']:
    bc_dict['MC'] = False
    bc_dict['MI'] = True
if args['--FT']:
    bc_dict['TT'] = False
    bc_dict['FT'] = True
elif args['--FF']:
    bc_dict['TT'] = False
    bc_dict['FF'] = True
if args['--FS']:
    bc_dict['NS'] = False
    bc_dict['FS'] = True
    

if threeD: resolution_flags = ['nx', 'ny', 'nz']
else:      resolution_flags = ['nx', 'nz']
data_dir = construct_out_dir(args, bc_dict, base_flags=['3D', 'Q', 'Ra', 'Pr', 'Pm', 'a'], frac_flags=['alp', 'β', 'logStep', 'Nboots'], label_flags=['noise_modes'], resolution_flags=resolution_flags, parent_dir_flag='root_dir')
logger.info("saving run in: {}".format(data_dir))

run_time_wall = float(args['--run_time_wall'])

mesh = args['--mesh']
if mesh is not None:
    mesh = mesh.split(',')
    mesh = [int(mesh[0]), int(mesh[1])]

### 2. Simulation parameters
Ra = float(args['--Ra'])
Pr = float(args['--Pr'])
Q  = float(args['--Q'])
Pm = float(args['--Pm'])
aspect = float(args['--a'])
nx = int(args['--nx'])
ny = int(args['--ny'])
nz = int(args['--nz'])


logger.info("Ra = {:.2e}, Pr = {:2g}, Q = {:.2e}, Pm = {:2g}, resolution = {}x{}x{}".format(Ra, Pr, Q, Pm, nx, ny, nz))

### 3. Setup Dedalus domain, problem, and substitutions/parameters
x_basis = de.Fourier( 'x', nx, interval = [-aspect/2, aspect/2], dealias=3/2)
if threeD:
    y_basis = de.Fourier( 'y', ny, interval = [-aspect/2, aspect/2], dealias=3/2)

z_basis = de.Chebyshev('z', nz, interval = [-1./2, 1./2], dealias=3/2)
if threeD:
    bases = [x_basis, y_basis, z_basis]
else:
    bases = [x_basis, z_basis]
domain = de.Domain(bases, grid_dtype=np.float64, mesh=mesh)

variables = ['T1','T1_z','p','u','w','phi','Ax','Ay','Az','Bx','By','Oy', 'p_ml', 'p_ml_z', 'p_mn', 'p_mn_z', 'p_i', 'p_i_z', 'p_b', 'p_b_z', 'p_v', 'p_v_z']
if threeD:
    variables+=['v','Ox']

problem = de.IVP(domain, variables=variables, ncc_cutoff=1e-10)

Q_fd  = domain.new_field()
Ra_fd = domain.new_field()
Q_fd['g']  = sQ  = cQ  = Q
Ra_fd['g'] = sRa = cRa = Ra

problem.parameters['Ra'] = Ra_fd
problem.parameters['Pr'] = Pr
problem.parameters['Pm'] = Pm
problem.parameters['Q']  = Q_fd
problem.parameters['pi'] = np.pi
problem.parameters['Lx'] = problem.parameters['Ly'] = aspect
problem.parameters['Lz'] = 1
problem.parameters['aspect'] = aspect
if not threeD:
    problem.substitutions['v']='0'
    problem.substitutions['dy(A)']='0'
    problem.substitutions['Oz']='0'
    problem.substitutions['Ox']='0'

problem.substitutions['T0']   = '(-z + 0.5)'
problem.substitutions['T0_z'] = '-1'
problem.substitutions['Lap(A, A_z)']=       '(dx(dx(A)) + dy(dy(A)) + dz(A_z))'
problem.substitutions['UdotGrad(A, A_z)'] = '(u*dx(A) + v*dy(A) + w*A_z)'
problem.substitutions['Div(Ax, Ay, Az)']  = '(dx(Ax) + dy(Ay) + dz(Az))'
problem.substitutions["Bz"] = "dx(Ay)-dy(Ax)"
problem.substitutions["Jx"] = "dy(Bz)-dz(By)"
problem.substitutions["Jy"] = "dz(Bx)-dx(Bz)"
problem.substitutions["Jz"] = "dx(By)-dy(Bx)"
problem.substitutions["Kz"] = "dx(Oy)-dy(Ox)"
problem.substitutions["Oz"] = "dx(v)-dy(u)"
problem.substitutions["Ky"] = "dz(Ox)-dx(Oz)"
problem.substitutions["Kx"] = "dy(Oz)-dz(Oy)"

#Dimensionless parameter substitutions
problem.substitutions["inv_Re_ff"]    = "(Pr/Ra)**(1./2.)"
problem.substitutions["inv_Rem_ff"]   = "(inv_Re_ff / Pm)"
problem.substitutions["M_alfven"]      = "sqrt((Ra*Pm)/(Q*Pr))"
problem.substitutions["inv_Pe_ff"]    = "(Ra*Pr)**(-1./2.)"

if threeD:
    problem.substitutions['plane_avg(A)'] = 'integ(A, "x", "y")/Lx/Ly'
    problem.substitutions['vol_avg(A)']   = 'integ(A)/Lx/Ly/Lz'
else:
    problem.substitutions['plane_avg(A)'] = 'integ(A, "x")/Lx'
    problem.substitutions['vol_avg(A)']   = 'integ(A)/Lx/Lz'
    
problem.substitutions['plane_std(A)'] = 'sqrt(plane_avg((A - plane_avg(A))**2))'
#put vol avg here rms vlaues
problem.substitutions['enstrophy']   = '(Ox**2 + Oy**2 + Oz**2)'
problem.substitutions['enth_flux']   = '(w*(T1+T0))'
problem.substitutions['cond_flux']   = '(-(T1_z+T0_z)/Pr)'
problem.substitutions['tot_flux']    = '(cond_flux+enth_flux)'
problem.substitutions['Nu']          = '((enth_flux + cond_flux)/vol_avg(cond_flux))'
problem.substitutions['delta_T']     = '(left(T1+T0)-right(T1+T0))'
problem.substitutions['vel_rms']     = 'sqrt(u**2 + v**2 + w**2)'
problem.substitutions['vel_rms_hor'] = 'sqrt(u**2 + v**2)'
problem.substitutions['ell']         = 'aspect/10'

problem.substitutions['Ex'] = 'dx(phi) + (1/Pm)*Jx + w*By       - v*(1 + Bz)'
problem.substitutions['Ey'] = 'dy(phi) + (1/Pm)*Jy + u*(1 + Bz) - w*Bx'
problem.substitutions['Ez'] = 'dz(phi) + (1/Pm)*Jz + v*Bx       - u*By'

problem.substitutions['f_v_x']   = 'Kx'
problem.substitutions['f_v_y']   = '0'
problem.substitutions['f_v_z']   = 'Kz'
problem.substitutions['f_i_x']   = 'v*Oz - w*Oy'
problem.substitutions['f_i_y']   = '0'
problem.substitutions['f_i_z']   = 'u*Oy - v*Ox'
problem.substitutions['f_ml_x']  = '(Q/Pr)*Jy'
problem.substitutions['f_ml_y']  = '0'
problem.substitutions['f_mn_x']  = '(Q/Pr)*(Jy*Bz - Jz*By)'
problem.substitutions['f_mn_y']  = '0'
problem.substitutions['f_mn_z']  = '(Q/Pr)*(Jx*By - Jy*Bx)'
problem.substitutions['f_b']     = '(Ra/Pr)*T1'

problem.substitutions['f_v_mag']  = 'sqrt(f_v_x**2 + f_v_z**2)'
problem.substitutions['f_ml_mag'] = 'sqrt(f_ml_x**2)'
problem.substitutions['f_i_mag']  = 'sqrt(f_i_x**2 + f_i_z**2)'
problem.substitutions['f_mn_mag'] = 'sqrt(f_mn_x**2 + f_mn_z**2)'
problem.substitutions['f_b_mag']  = 'sqrt(f_b**2)'

problem.substitutions['s_v_mag']  = 'sqrt((f_v_x  - dx(p_v) )**2 + (f_v_z  - dz(p_v) )**2)'
problem.substitutions['s_ml_mag'] = 'sqrt((f_ml_x - dx(p_ml))**2 +          (dz(p_ml))**2)'
problem.substitutions['s_i_mag']  = 'sqrt((f_i_x  - dx(p_i) )**2 + (f_i_z  - dz(p_i) )**2)'
problem.substitutions['s_mn_mag'] = 'sqrt((f_mn_x - dx(p_mn))**2 + (f_mn_z - dz(p_mn))**2)'
problem.substitutions['s_b_mag']  = 'sqrt(         (dx(p_b) )**2 + (f_b    - dz(p_b) )**2)'


problem.substitutions['Re']           = '( vel_rms )'
problem.substitutions['Pe']           = '( vel_rms )'
problem.substitutions['Re_ver']       = '( sqrt(w**2) )'
problem.substitutions['Re_hor']       = '( vel_rms_hor * ell)'
problem.substitutions['Re_hor_full']  = '(vel_rms * ell)'
problem.substitutions['b_mag']        = 'sqrt(Bx**2 + By**2 + Bz**2)'
problem.substitutions['b_perp']       = 'sqrt(Bx**2 + By**2)'
problem.substitutions['gp_mag']       = 'sqrt(dx(p)**2 + dz(p)**2)'
problem.substitutions['mod_f_ml_mag'] = 'sqrt(dx(p)**2 - f_ml_x**2)'

### 4.Setup equations and Boundary Conditions
if threeD:
    zero_cond = "(nx == 0) and (ny == 0)"
    else_cond = "(nx != 0) or  (ny != 0)"
else:
    zero_cond = "(nx == 0)"
    else_cond = "(nx != 0)"



eqns = (
        (True,   "dt(T1) + w*T0_z   - (1/Pr)*Lap(T1, T1_z) = -UdotGrad(T1, T1_z)"),
        (True,   "dt(u)  + dx(p)   + f_v_x  =       f_i_x + f_ml_x + f_mn_x"),
        (threeD, "dt(v)  + dy(p)   + f_v_y  =       f_i_y + f_ml_y + f_mn_y "),
        (True,   "dt(w)  + dz(p)   + f_v_z  = f_b + f_i_z +          f_mn_z "),
        (True,   "dt(Ax) + dx(phi) + (1/Pm)*Jx - v             = v*Bz - w*By"),
        (True,   "dt(Ay) + dy(phi) + (1/Pm)*Jy + u             = w*Bx - u*Bz"),
        (True,   "dt(Az) + dz(phi) + (1/Pm)*Jz                 = u*By - v*Bx"),
        (True,   "dx(u)  + dy(v)  + dz(w)  = 0"),
        (True,   "dx(Ax) + dy(Ay) + dz(Az) = 0"),
        (True,   "Bx - (dy(Az) - dz(Ay)) = 0"),
        (True,   "By - (dz(Ax) - dx(Az)) = 0"),
        (threeD, "Ox - (dy(w) - dz(v)) = 0"),
        (True,   "Oy - (dz(u) - dx(w)) = 0"),
        (True,   "T1_z - dz(T1) = 0"),
        (True,   "Lap(p_b,  p_b_z)  = Div(0,      0,      f_b)"),
        (True,   "Lap(p_ml, p_ml_z) = Div(f_ml_x, f_ml_y, 0)"),
        (True,   "Lap(p_mn, p_mn_z) = Div(f_mn_x, f_mn_y, f_mn_z)"),
        (True,   "Lap(p_i,  p_i_z)  = Div(f_i_x,  f_i_y,  f_i_z)"),
        (True,   "Lap(p_v,  p_v_z)  = 0"),
        (True,   "p_b_z -  dz(p_b) = 0"),
        (True,   "p_ml_z - dz(p_ml) = 0"),
        (True,   "p_mn_z - dz(p_mn) = 0"),
        (True,   "p_i_z -  dz(p_i) = 0"),
        (True,   "p_v_z -  dz(p_v) = 0"),
      )


for do_eqn, eqn in eqns:
    if do_eqn:
        problem.add_equation(eqn)

bcs  = (
            (bc_dict['FF'],        " left(T1_z)     = 0", "True"),
            (bc_dict['FF'],        "right(T1_z)     = 0", "True"),
            (bc_dict['FT'],        " left(T1_z)     = 0", "True"),
            (bc_dict['FT'],        "right(T1)       = 0", "True"),
            (bc_dict['TT'],        " left(T1)       = 0", "True"),
            (bc_dict['TT'],        "right(T1)       = 0", "True"),
            (bc_dict['FS'],        " left(Oy)       = 0", "True"),
            (bc_dict['FS'],        "right(Oy)       = 0", "True"),
            (bc_dict['FS']*threeD, " left(Ox)       = 0", "True"),
            (bc_dict['FS']*threeD, "right(Ox)       = 0", "True"),
            (bc_dict['NS'],        " left(u)        = 0", "True"),
            (bc_dict['NS'],        "right(u)        = 0", "True"),
            (bc_dict['NS']*threeD, " left(v)        = 0", "True"),
            (bc_dict['NS']*threeD, "right(v)        = 0", "True"),
            (True,                 " left(w)        = 0", "True"),
            (True,                 "right(p)        = 0", zero_cond),
            (True,                 "right(w)        = 0", else_cond),
            (bc_dict['MI'],        " left(Bx)       = 0", "True"),
            (bc_dict['MI'],        "right(Bx)       = 0", "True"),
            (bc_dict['MI'],        " left(By)       = 0", "True"),
            (bc_dict['MI'],        "right(By)       = 0", "True"),
            (bc_dict['MI'],        " left(Az)       = 0", "True"),
            (bc_dict['MI'],        "right(Az)       = 0", else_cond),
            (bc_dict['MI'],        "right(phi)      = 0", zero_cond),
            (bc_dict['MC'],        " left(Ax)       = 0", "True"),
            (bc_dict['MC'],        "right(Ax)       = 0", "True"),
            (bc_dict['MC'],        " left(Ay)       = 0", "True"),
            (bc_dict['MC'],        "right(Ay)       = 0", "True"),
            (bc_dict['MC'],        " left(phi)      = 0", "True"),
            (bc_dict['MC'],        "right(phi)      = 0", else_cond),
            (bc_dict['MC'],        "right(Az)       = 0", zero_cond),
            (True,                 " left(dz(p_b))  =  left(f_b)",   "True"),
            (True,                 "right(dz(p_b))  = right(f_b)",   else_cond),
            (True,                 "right(p_b)      = 0",            zero_cond),
            (True,                 " left(dz(p_i))  =  left(f_i_z)", "True"),
            (True,                 "right(dz(p_i))  = right(f_i_z)", else_cond),
            (True,                 "right(p_i)      = 0",            zero_cond),
            (True,                 " left(dz(p_ml)) = 0",            "True"),
            (True,                 "right(dz(p_ml)) = 0",            else_cond),
            (True,                 "right(p_ml)     = 0",            zero_cond),
            (True,                 " left(dz(p_mn)) =  left(f_mn_z)", "True"),
            (True,                 "right(dz(p_mn)) = right(f_mn_z)", else_cond),
            (True,                 "right(p_mn)     = 0",             zero_cond),
            (True,                 " left(dz(p_v))  =  left(f_v_z)", "True"),
            (True,                 "right(dz(p_v))  = right(f_v_z)", else_cond),
            (True,                 "right(p_v)      = 0",            zero_cond),
          )

for do_bc, bc, cond in bcs:
    if do_bc:
        problem.add_bc(bc, condition=cond)

### 5. Build solver
# Note: SBDF2 timestepper does not currently work with AE

if args['--SBDF2']:
    ts = de.timesteppers.SBDF2
if args['--SBDF4']:
    ts = de.timesteppers.SBDF4
else:
    ts = de.timesteppers.RK443


cfl_safety = float(args['--safety'])
solver = problem.build_solver(ts)
logger.info('Solver built')


### 6. Set initial conditions: noise or loaded checkpoint
checkpoint = Checkpoint(data_dir)
checkpoint_min = 30
restart = args['--restart']
if restart is None:
    p = solver.state['p']
    T1 = solver.state['T1']
    T1_z = solver.state['T1_z']
    p.set_scales(domain.dealias)
    T1.set_scales(domain.dealias)
    T1_z.set_scales(domain.dealias)
    z_de = domain.grid(-1, scales=domain.dealias)

    A0 = 1e-6

    #Add noise kick
    noise = global_noise(domain, int(args['--seed']))
    T1['g'] += A0*np.cos(np.pi*z_de)*noise['g']#/np.sqrt(Ra)
    T1.differentiate('z', out=T1_z)


    dt = None
    mode = 'overwrite'
else:
    logger.info("restarting from {}".format(restart))
    dt = checkpoint.restart(restart, solver)
    mode = 'append'
checkpoint.set_checkpoint(solver, wall_dt=checkpoint_min*60, mode=mode, iter=5e3)
   

### 7. Set simulation stop parameters, output, and CFL
solver.stop_sim_time = np.inf
solver.stop_wall_time = run_time_wall*3600.
t_buoy = np.sqrt(Pr/Ra)
f=float(args['--factor'])
max_dt    = 0.5*np.min((t_buoy, f/Q))
if dt is None: dt = max_dt
analysis_tasks = initialize_magnetic_output(solver, data_dir, aspect, plot_boundaries=False, threeD=threeD, mode=mode, slice_output_dt=0.25*t_buoy, output_dt=0.1*t_buoy, out_iter=100)

#Add extra analysis tasks
analysis_tasks['scalar'].add_task("1 - vol_avg(p)/vol_avg(p_i + p_b + p_v + p_mn + p_ml)", name="p_goodness")
analysis_tasks['scalar'].add_task("vol_avg(sqrt(p_i**2))", name="p_i")
analysis_tasks['scalar'].add_task("vol_avg(sqrt(p_b**2))", name="p_b")
analysis_tasks['scalar'].add_task("vol_avg(sqrt(p_v**2))", name="p_v")
analysis_tasks['scalar'].add_task("vol_avg(sqrt(p_ml**2))", name="p_ml")
analysis_tasks['scalar'].add_task("vol_avg(sqrt(p_mn**2))", name="p_mn")
analysis_tasks['scalar'].add_task("vol_avg(s_v_mag)", name="s_v_mag")
analysis_tasks['scalar'].add_task("vol_avg(s_i_mag)", name="s_i_mag")
analysis_tasks['scalar'].add_task("vol_avg(s_b_mag)", name="s_b_mag")
analysis_tasks['scalar'].add_task("vol_avg(s_mn_mag)", name="s_mn_mag")
analysis_tasks['scalar'].add_task("vol_avg(s_ml_mag)", name="s_ml_mag")
analysis_tasks['scalar'].add_task("vol_avg(Ra)", name="Ra")
analysis_tasks['scalar'].add_task("vol_avg(Q)",  name="Q")


# CFL
CFL = flow_tools.CFL(solver, initial_dt=dt, cadence=1, safety=cfl_safety,
                     max_change=1.5, min_change=0.5, max_dt=max_dt, threshold=0.1)
if threeD:
    CFL.add_velocities(('u', 'v', 'w'))
else:
    CFL.add_velocities(('u', 'w'))

    
### 8. Setup flow tracking for terminal output, including rolling averages
flow = flow_tools.GlobalFlowProperty(solver, cadence=1)
flow.add_property("s_b_mag",  name='s_b_mag')
flow.add_property("s_i_mag",  name='s_i_mag')
flow.add_property("s_v_mag",  name='s_v_mag')
flow.add_property("s_mn_mag", name='s_mn_mag')
flow.add_property("s_ml_mag", name='s_ml_mag')
flow.add_property("Re", name='Re')
flow.add_property("Re_ver", name='Re_ver')
flow.add_property("Re_hor", name='Re_hor') 
flow.add_property("Re_hor_full", name='Re_hor_full')
flow.add_property("b_mag", name="b_mag")
flow.add_property("sqrt(Bz**2)", name="Bz")
flow.add_property("dx(Bx) + dy(By) + dz(Bz)", name='divB')
flow.add_property("Nu", name='Nu')
#flow.add_property("-1 + (left(T1_z) + right(T1_z) ) / 2", name='T1_z_excess')
#flow.add_property("T0+T1", name='T')


if threeD:
    Hermitian_cadence = 100

# Bootstrap tracking fields.
u = solver.state['u']
w = solver.state['w']
maxN = int(4e3)
bootstrap_force_balances = np.zeros((maxN, 4))
rolled = np.zeros_like(bootstrap_force_balances)
bootstrap_df = DataFrame(bootstrap_force_balances)
bootstrap_i         = 0
last_bootstrap_time = 0
last_bootstrap_write_time = 0
bootstrap_now       = False
bootstrap_wait_time = 20*t_buoy
bootstrap_min_iters = int(2*(float(args['--boot_time']) - 50))
max_bootstrap_steps = int(args['--Nboots'])
bootstrap_steps     = 0

bootstrap_α = float(Fraction(args['--alp']))
bootstrap_β = float(Fraction(args['--β']))
bootstrap_logStep = float(Fraction(args['--logStep']))
    
# Main loop
try:
    Re_avg = 0
    #logger.info('Starting loop')
    #not_corrected_times = True
    init_time = last_time = solver.sim_time
    start_iter = solver.iteration
    start_time = time.time()
    #avg_nu = avg_temp = avg_T1_z = 0
    while (solver.ok and np.isfinite(Re_avg)):


        dt = CFL.compute_dt()
        solver.step(dt) #, trim=True)


        # Solve for blow-up over long timescales in 3D due to hermitian-ness
        effective_iter = solver.iteration - start_iter
        if threeD:
            if effective_iter % Hermitian_cadence == 0:
                for field in solver.state.fields:
                    field.require_grid_space()
    
                    
        if effective_iter % 10 == 0:
            Re_avg = flow.grid_average('Re')
            Re_avg_ver = flow.grid_average('Re_ver') 
            Re_avg_hor = flow.grid_average('Re_hor') 
            Re_avg_hor_full = flow.grid_average('Re_hor_full') 
            log_string =  'Iteration: {:5d}, '.format(solver.iteration)
            log_string += 'Time: {:8.3e} ({:8.3e} buoy), dt: {:8.3e}, '.format(solver.sim_time, solver.sim_time/t_buoy,  dt)
            log_string += 'Re: {:8.3e}/{:8.3e}, '.format(Re_avg, flow.max('Re'))
            log_string += 'Re_ver: {:8.3e}/{:8.3e}, '.format(Re_avg_ver, flow.max('Re_ver'))
            log_string += 'Re_hor: {:8.3e}/{:8.3e}, '.format(Re_avg_hor, flow.max('Re_hor'))
            log_string += 'Re_hor_full: {:8.3e}/{:8.3e}, '.format(Re_avg_hor_full, flow.max('Re_hor_full'))
            log_string += 'Bz: {:8.3e}/{:8.3e}, '.format(flow.grid_average('Bz'), flow.max('Bz'))
            log_string += 'b_mag: {:8.3e}/{:8.3e}, '.format(flow.grid_average('b_mag'), flow.max('b_mag'))
            log_string += 'divB: {:8.3e}, '.format(flow.grid_average('divB'))
            log_string += 'Nu: {:8.3e}, '.format(flow.grid_average('Nu'))
            logger.info(log_string)

       # if Re_avg < 1:
       #     last_bootstrap_time = solver.sim_time
       #     last_bootstrap_write_time = solver.sim_time
       # elif (solver.sim_time - last_bootstrap_write_time > 0.5*t_buoy) and (solver.sim_time - last_bootstrap_time > bootstrap_wait_time):
       #     # Add a write every 0.5 t_ff
       #     s_b_mag = flow.grid_average('s_b_mag')
       #     bootstrap_force_balances[bootstrap_i,:] = (s_b_mag/flow.grid_average('s_i_mag'), s_b_mag/flow.grid_average('s_mn_mag'), s_b_mag/flow.grid_average('s_ml_mag'), s_b_mag/flow.grid_average('s_v_mag'))
       #     if bootstrap_i >= bootstrap_min_iters:
       #         rolled = np.array(bootstrap_df.rolling(window=maxN, min_periods=int(bootstrap_min_iters/2)).mean())
       #         rms_chunk = rolled[bootstrap_i-int(bootstrap_min_iters/2):bootstrap_i]
       #         rms_vals  = np.sqrt(np.mean((rms_chunk - rolled[bootstrap_i])**2/rolled[bootstrap_i]**2, axis=0))
       #         logger.info('max bootstrap RMS: {:.3e}, need 0.01'.format(np.max(rms_vals)))
       #         if np.max(rms_vals) < 0.01:
       #             bootstrap_now = True
       #     bootstrap_i += 1
       #     if bootstrap_i == maxN:
       #         bootstrap_now = True
       #     last_bootstrap_write_time = solver.sim_time
        if Re_avg < 1:
            last_bootstrap_time = solver.sim_time
        elif solver.sim_time - last_bootstrap_time > bootstrap_wait_time:
            bootstrap_now = True
             

        if bootstrap_now:
            if bootstrap_steps == max_bootstrap_steps:
                logger.info("Finished bootstrap run")
                break
            else:
                bootstrap_now = False
                bootstrap_steps += 1
            if bootstrap_β == 0:
                nRa = cRa*10**(bootstrap_logStep)
                nQ = cQ
            elif bootstrap_α == 0:
                nQ = cQ*10**(bootstrap_logStep)
                nRa = cRa
            else:
                nRa = cRa*10**(bootstrap_logStep)
                nQ = cQ/(10**(bootstrap_logStep))**(bootstrap_α/bootstrap_β)

            logger.info('bootstrapping Ra: {:.3e}->{:.3e}, Q: {:.3e} -> {:.3e}'.format(cRa, nRa, cQ, nQ))
            Ra_fd['g'] *= (nRa/cRa)
            Q_fd['g']  *= (nQ/cQ)

            u_factor = np.sqrt(nRa/cRa)
            u['g'] *= u_factor
            w['g'] *= u_factor

            bootstrap_wait_time *= (cRa/nRa)
            max_dt *= (cRa/nRa)
            CFL.max_dt=max_dt
           # CFL = flow_tools.CFL(solver, initial_dt=dt, cadence=1, safety=cfl_safety,
           #                        max_change=1.5, min_change=0.5, max_dt=max_dt, threshold=0.1)
            if threeD: 
                CFL.add_velocities(('u', 'v', 'w'))
            else: 
                CFL.add_velocities(('u', 'w'))

            cQ = nQ
            cRa = nRa

            t_buoy = np.sqrt(Pr/cRa)

            bootstrap_force_balances *= 0
            rolled *= 0
            bootstrap_i = 0
            last_bootstrap_time = solver.sim_time

except:
    raise
    logger.error('Exception raised, triggering end of main loop.')
finally:
    end_time = time.time()
    main_loop_time = end_time-start_time
    n_iter_loop = solver.iteration-1
    logger.info('Iterations: {:d}'.format(n_iter_loop))
    logger.info('Sim end time: {:f}'.format(solver.sim_time))
    logger.info('Run time: {:f} sec'.format(main_loop_time))
    logger.info('Run time: {:f} cpu-hr'.format(main_loop_time/60/60*domain.dist.comm_cart.size))
    logger.info('iter/sec: {:f} (main loop only)'.format(n_iter_loop/main_loop_time))
    try:
        final_checkpoint = Checkpoint(data_dir, checkpoint_name='final_checkpoint')
        final_checkpoint.set_checkpoint(solver, wall_dt=1, mode=mode)
        solver.step(dt) #clean this up in the future...works for now.
        post.merge_process_files(data_dir+'/final_checkpoint/', cleanup=False)
    except:
        raise
        print('cannot save final checkpoint')
    finally:
        if not args['--no_join']:
            logger.info('beginning join operation')
            post.merge_analysis(data_dir+'checkpoint')

            for key, task in analysis_tasks.items():
                logger.info(task.base_path)
                post.merge_analysis(task.base_path)

        logger.info(40*"=")
        logger.info('Iterations: {:d}'.format(n_iter_loop))
        logger.info('Sim end time: {:f}'.format(solver.sim_time))
        logger.info('Run time: {:f} sec'.format(main_loop_time))
        logger.info('Run time: {:f} cpu-hr'.format(main_loop_time/60/60*domain.dist.comm_cart.size))
        logger.info('iter/sec: {:f} (main loop only)'.format(n_iter_loop/main_loop_time))
