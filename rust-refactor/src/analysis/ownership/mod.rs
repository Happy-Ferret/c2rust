//! This module contains an analysis to infer ownership information for pointers.  It analyzes code
//! using raw pointers and indicates, for each pointer, whether it appears to be owned, mutably
//! borrowed, or immutably borrowed.  It can also infer ownership-polymorphic function signatures,
//! which handles cases where the original C code used a single accessor for both mutable and
//! immutable access to a field.
//!
//! The analysis operates on constraint sets over "permission variables", which can be take on the
//! concrete permissions "READ", "WRITE", and "MOVE".  The analysis runs in two phases.  First, for
//! each function, it analyzes the function and produces a set of constraints relating variables in
//! the function's signature, variables appearing in static locations (such as struct field types).
//! Since interprocedural information is not available yet, this phase leaves holes where
//! constraints for callee functions can be plugged in.  The second phase fills in holes in
//! function summaries to produce complete summaries that are useful to analysis consumers.  It
//! runs interprocedurally to a fixed point, on each function plugging in the complete summaries of
//! its callees and simplifying to produce a complete summary for the current function.

use std::cmp;
use std::collections::Bound;
use std::collections::BTreeSet;
use std::collections::hash_map::{HashMap, Entry};
use std::collections::HashSet;
use std::collections::VecDeque;
use std::fmt;
use std::u32;

use arena::DroplessArena;
use rustc::hir;
use rustc::hir::def_id::{DefId, LOCAL_CRATE};
use rustc::mir::*;
use rustc::mir::tcx::LvalueTy;
use rustc::mir::traversal::{Postorder, ReversePostorder};
use rustc::ty::{Ty, TyS, TyCtxt, FnSig, Instance, TypeVariants, AdtDef};
use rustc::ty::subst::Substs;
use rustc::ty::fold::{TypeVisitor, TypeFoldable};
use rustc_data_structures::bitvec::BitVector;
use rustc_data_structures::indexed_vec::{IndexVec, Idx};
use syntax::ast;

use analysis::labeled_ty::{LabeledTy, LabeledTyCtxt};
use command::CommandState;
use driver;
use type_map::{self, TypeSource};


pub mod constraint;
mod context;
mod intra;
mod inter;
mod annot;
mod mono;
mod inst;
mod mono_filter;
mod debug;

use self::constraint::*;
use self::context::Ctxt;
use self::intra::IntraCtxt;
use self::inter::InterCtxt;
use self::annot::handle_marks;
use self::mono::{mono_test, get_all_mono_sigs};
use self::inst::find_instantiations;
use self::mono_filter::filter_suspicious_monos;
use self::debug::*;


#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Debug, Hash)]
pub struct Var(u32);

impl Idx for Var {
    fn new(idx: usize) -> Var {
        assert!(idx as u32 as usize == idx);
        Var(idx as u32)
    }

    fn index(self) -> usize {
        self.0 as usize
    }
}

pub type LTy<'tcx> = LabeledTy<'tcx, Option<Perm<'tcx>>>;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct LFnSig<'tcx> {
    pub inputs: &'tcx [LTy<'tcx>],
    pub output: LTy<'tcx>,
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Debug, Hash)]
pub enum ConcretePerm {
    Read,
    Write,
    Move,
}


pub struct FnSummary<'tcx> {
    sig: LFnSig<'tcx>,
    num_sig_vars: u32,
    cset: ConstraintSet<'tcx>,
    inst_cset: ConstraintSet<'tcx>,
    insts: Vec<Instantiation>,
}

struct Instantiation {
    callee: DefId,
    first_inst_var: u32,
}






fn is_fn(hir_map: &hir::map::Map, def_id: DefId) -> bool {
    use rustc::hir::map::Node::*;

    let n = match hir_map.get_if_local(def_id) {
        None => return false,
        Some(n) => n,
    };

    match n {
        NodeItem(i) => match i.node {
            hir::ItemFn(..) => true,
            _ => false,
        },
        NodeForeignItem(i) => match i.node {
            hir::ForeignItemFn(..) => true,
            _ => false,
        },
        NodeTraitItem(i) => match i.node {
            hir::TraitItemKind::Method(..) => true,
            _ => false,
        },
        NodeImplItem(i) => match i.node {
            hir::ImplItemKind::Method(..) => true,
            _ => false,
        },
        _ => false,
    }
}

fn analyze_intra<'a, 'gcx, 'tcx>(cx: &mut Ctxt<'tcx>,
                                 hir_map: &hir::map::Map,
                                 tcx: TyCtxt<'a, 'gcx, 'tcx>) {
    for &def_id in tcx.mir_keys(LOCAL_CRATE).iter() {
        // We currently don't process `static` bodies, even though they do have MIR.
        if !is_fn(hir_map, def_id) {
            continue;
        }

        let mir = tcx.optimized_mir(def_id);

        let mut local_cx = IntraCtxt::new(cx, tcx, def_id, mir);
        local_cx.init();

        for (bbid, bb) in mir.basic_blocks().iter_enumerated() {
            local_cx.handle_basic_block(bbid, bb);
        }

        local_cx.finish();
    }
}

fn analyze_inter(cx: &mut Ctxt) {
    let mut inter_cx = InterCtxt::new(cx);
    inter_cx.process();
    inter_cx.finish();
}


pub struct FunctionResult<'tcx> {
    /// Polymorphic function signature.  Each pointer is labeled with a `SigVar`.
    pub sig: LFnSig<'tcx>,

    pub num_sig_vars: u32,

    /// Constraint set relating `SigVar`s to each other and to concrete permission values.
    pub cset: ConstraintSet<'tcx>,

    /// Monomorphizations
    pub monos: Vec<MonoResult>,

    /// IDs of all referenced functions.  (This includes both callees and functions whose address
    /// is taken within the current function.)
    pub callee_ids: Vec<DefId>,
}

pub struct MonoResult {
    /// Suffix to add to the function name, such as `mut` or `take`.
    pub suffix: String,

    /// Assignment of concrete permission values to the signature variables of the function.  All
    /// monomorphizations use the same `sig`, but have different assignments to its `SigVar`s.
    pub assign: IndexVec<Var, ConcretePerm>,

    /// Index of the chosen callee monomorphization for each call site.  These correspond to the
    /// IDs in `callee_ids`.
    pub callee_mono_idxs: Vec<usize>,
}

pub fn analyze<'a, 'hir, 'gcx, 'tcx>(st: &CommandState,
                                     dcx: &driver::Ctxt<'a, 'hir, 'gcx, 'tcx>)
                                     -> HashMap<DefId, FunctionResult<'tcx>> {
    let mut cx = Ctxt::new(dcx.ty_arena());

    handle_marks(&mut cx, st, dcx);

    // Compute constraints for each function
    analyze_intra(&mut cx, dcx.hir_map(), dcx.ty_ctxt());
    analyze_inter(&mut cx);

    // Monomorphize functions and call sites
    let mono_sigs = get_all_mono_sigs(&cx);
    let inst_sel = find_instantiations(&cx, &mono_sigs);

    let mono_filter = filter_suspicious_monos(&cx, &mono_sigs, &inst_sel);


    // Map from original (callee_id, mono_idx) to the mono_idx after filtering.
    let mut filtered_mono_idx = HashMap::new();
    for (&def_id, mono_sigs) in &mono_sigs {
        let mut j = 0;
        for i in 0 .. mono_sigs.len() {
            if mono_filter.contains(&(def_id, i)) {
                continue;
            }
            filtered_mono_idx.insert((def_id, i), j);
            j += 1;
        }
    }

    // Build the final result map
    let mut results = HashMap::new();
    for (&def_id, mono_sigs) in &mono_sigs {
        let summ = cx.get_fn_summ_imm(def_id).unwrap();

        let mut mono_results = Vec::new();
        for (i, mono_sig) in mono_sigs.iter().enumerate() {
            if mono_filter.contains(&(def_id, i)) {
                continue;
            }

            let orig_inst_sel = &inst_sel[&(def_id, i)];
            let mut inst_sel = Vec::with_capacity(orig_inst_sel.len());
            for (inst, &mono_idx) in summ.insts.iter().zip(orig_inst_sel.iter()) {
                inst_sel.push(filtered_mono_idx[&(inst.callee, mono_idx)]);
            }

            mono_results.push(MonoResult {
                // TODO: be smarter about naming.  try "" / "mut" / "take" for R/W/M variants
                suffix: if mono_sigs.len() > 0 { format!("{}", i) } else { format!("") },
                assign: mono_sig.clone(),
                callee_mono_idxs: inst_sel,
            });
        }

        let callee_ids = summ.insts.iter().map(|inst| inst.callee).collect();

        results.insert(def_id, FunctionResult {
            sig: summ.sig,
            num_sig_vars: summ.num_sig_vars,
            cset: summ.cset.clone(),
            monos: mono_results,
            callee_ids: callee_ids,
        });
    }

    results
}

pub fn dump_results(dcx: &driver::Ctxt,
                    results: &HashMap<DefId, FunctionResult>) {
    eprintln!("\n === summary ===");

    let arena = DroplessArena::new();
    let mut new_lcx = LabeledTyCtxt::new(&arena);
    let format_sig = |sig: LFnSig, assign: &IndexVec<Var, ConcretePerm>| {
        let mut func = |p: &Option<_>| {
            if let Some(Perm::SigVar(v)) = *p {
                Some(assign[v])
            } else {
                None
            }
        };

        let inputs = new_lcx.relabel_slice(sig.inputs, &mut func);
        let output = new_lcx.relabel(sig.output, &mut func);
        format!("{:?} -> {:?}", pretty_slice(inputs), Pretty(output))
    };

    let path_str = |def_id| dcx.ty_ctxt().def_path(def_id).to_string(dcx.ty_ctxt());

    let mut ids = results.keys().cloned().collect::<Vec<_>>();
    ids.sort();
    for id in ids {
        let fr = &results[&id];

        eprintln!("fn {}:", path_str(id));
        for (i, mr) in fr.monos.iter().enumerate() {
            eprintln!("  mono #{}: {}", i, format_sig(fr.sig, &mr.assign));
            for (j, (&callee, &mono_idx)) in
                    fr.callee_ids.iter().zip(mr.callee_mono_idxs.iter()).enumerate() {
                let callee_fr = &results[&callee];
                eprintln!("    call #{}: {:?} #{} :: {}",
                          j, path_str(callee), mono_idx,
                          format_sig(callee_fr.sig, &callee_fr.monos[mono_idx].assign));
            }
        }
    }
}