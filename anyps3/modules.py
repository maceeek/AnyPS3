"""Static firmware module catalog and native entry/return transitions.

All catalog entries must have real relocated images and exports. Missing
modules or unsupported module-start results trap; no success placeholders.
"""

from .errors import AnyPS3Error
import json

MODULE_RETURN = 0xfffe0000


def module_lookup(modules):
    lines = ['[[maybe_unused]] static void native_module_by_name(PpuState& state) {',
             'auto& r=state.r;if(r[4] || r[5] || !r[3]){r[3]=sign_word(MM_EINVAL);return;}',
             'std::string name;for(unsigned i=0;i<28;++i){const auto c=state.memory.load(r[3]+i,1);if(!c)break;name+=static_cast<char>(c);}',
             'if(name.size()==28){r[3]=sign_word(MM_EINVAL);return;}']
    for i,module in enumerate(modules):
        loaded=module_available(module)
        name=json.dumps(module.get('module_name',module['name']))
        lines.append(f'if(name=={name} && ({loaded})){{r[3]=0x{0x23000000+i:x};return;}}')
    lines += ['r[3]=sign_word(0x8001112e);', '}']
    return '\n'.join(lines)


def validate_catalog(image, modules):
    ids = set()
    for module in modules:
        if module.get('resident') and module['ids']:
            raise AnyPS3Error('Resident module must have no load IDs.')
        if not module['ids'] and not module.get('resident'):
            raise AnyPS3Error('Native module has no ID.')
        for ident in module['ids']:
            if type(ident) is not int or not 0 <= ident < 65536 or ident in ids:
                raise AnyPS3Error('Invalid or duplicate native module ID.')
            ids.add(ident)
        if module.get('start'):
            image.resolve_entry(address=module['start'])
    try:
        image.read_virtual(MODULE_RETURN,4)
    except AnyPS3Error:
        return
    raise AnyPS3Error('Native module return address overlaps the input image.')


def module_available(module):
    if module.get('resident'):
        return f'state.initialized_residents.count({json.dumps(module["name"])})' if module.get('start') else 'true'
    return ' || '.join(f'state.loaded_modules.count({ident})' for ident in module['ids'])


def resident_startup(image, modules):
    lines=[]
    for module in modules:
        if not module.get('resident') or not module.get('start'):
            continue
        code,toc=image.resolve_entry(address=module['start'])
        lines.extend(['{ const auto saved=static_cast<PpuContext&>(state);',
                      f'state.pc=0x{code:x};state.r[2]=0x{toc:x};state.r[3]=0;state.r[4]=0;state.lr=RETURN_SENTINEL;',
                      'state.module_loading=true;anyps3_entry(state,trace);',
                      'if(state.r[3])throw std::runtime_error("resident firmware module_start failed");',
                      f'state.initialized_residents.insert({json.dumps(module["name"])});',
                      f'std::fprintf(stderr,"resident module started: %s\\n",{json.dumps(module["name"])});',
                      'state.module_loading=false;static_cast<PpuContext&>(state)=saved; }'])
    return '\n'.join(lines)


def module_runtime(image, modules):
    lines = ['[[maybe_unused]] static void native_module_load(PpuState& state) {',
             '    const auto id = state.r[3] & 0xffff;',
             '    if (state.loaded_modules.count(id)) { state.r[3] = 0x80012001; state.pc = checked_target(state.lr); return; }',
             '    std::uint32_t entry = 0, toc = 0;', '    switch (id) {']
    for module in modules:
        if module.get('resident'):
            continue
        for ident in module['ids']:
            lines.append(f'    case {ident}:')
        code,toc = image.resolve_entry(address=module['start']) if module.get('start') else (0,0)
        lines.append(f'        entry = 0x{code:x}; toc = 0x{toc:x}; break;')
    lines += ['    default: throw std::runtime_error("no statically linked firmware module for ID " + std::to_string(id));',
              '    }',
              '    std::fprintf(stderr,"native module load id=%u entry=0x%08x\\n",unsigned(id),entry);',
              '    if (!entry) { state.loaded_modules.insert(id); state.r[3]=0; state.pc=checked_target(state.lr); return; }',
              '    if (state.module_loading) throw std::runtime_error("nested module initialization is unsupported");',
              '    state.module_loading=true; state.module_id=id; state.module_lr=state.lr; state.module_toc=state.r[2];',
              f'    state.r[2]=toc; state.r[3]=0; state.r[4]=0; state.lr=0x{MODULE_RETURN:x}; state.pc=entry;',
              '}',
              'static void native_module_return(PpuState& state) {',
              '    if (!state.module_loading) throw std::runtime_error("unexpected native module return");',
              '    if (state.r[3]) throw std::runtime_error("unsupported/nonresident firmware module start result " + std::to_string(state.r[3]));',
              '    state.loaded_modules.insert(state.module_id); state.module_loading=false;',
              '    state.lr=state.module_lr; state.r[2]=state.module_toc; state.pc=checked_target(state.lr);',
              '}', '']
    return '\n'.join(lines)


def exported_bindings(modules):
    result = {}
    for module in modules:
        for library in module['exports']:
            if not library['library'] or not library['attributes'] & 1:
                continue
            if library['variable_count'] or library['tls_variable_count']:
                raise AnyPS3Error('Native variable exports require additional linkage support.')
            for function in library['functions']:
                key = (library['library'],function['nid'])
                if key in result:
                    raise AnyPS3Error('Ambiguous native firmware export.')
                result[key] = (module, function)
    return result
