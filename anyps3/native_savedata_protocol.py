"""Firmware 3.01 SAVE wire catalogue; addresses are bundle-338 guest addresses.

Conditions describe the original client, not permissive XML defaults. The
extended client is relocated to 0x30000000 for research, not game integration.
"""
I = 'int32'
S = 'string'

def entry(address, required, optional=None, packets=(), **extra):
    return dict(address=address, required=required, conditional=optional or {},
                response_packets=list(packets), **extra)

PROTOCOL = {
 'getOwnerShip': entry('libsysutil:0x2011c988; savedata:0x300024cc',
    {'elmUtilType': I, 'elmUtilOption': I}, packets=['resGetOwnerShip(elmResult:int32)']),
 'loadModules': entry('libsysutil:0x20120a00; savedata:0x30001abc',
    {'elmUtilType': I, 'elmContainer':4}, packets=['resLoadModules(elmResult:int32)']),
 'unloadModules': entry('libsysutil:0x20120b50; savedata:0x3000267c',
    {'elmUtilType': I}, packets=['resUnloadModules(elmResult:int32)'],
    branches={'terminal':'common abort reply -> unloadModules(type14), owner transaction retired', 'active':'explicit module unload in extended client'},
    evidence=['0x2011f19c li r4,14; 0x2011f1a4 bl 0x20120b50']),
 'getDirList': entry('libsysutil:0x2011ce74,0x2011ef20; savedata:0x30000608',
    {'elmUserId': I,'elmSearchType': I,'elmSortType':I,'elmSortOrder':I,
     'elmDirNamePlefix':S,'elmDirListNumMax':I},
    packets=['resGetDirList(elmResult,elmDirNum,elmListedDirNum)', 'if listed>0: raw 48*listed bytes'],
    branches={'search0':'prefix list, initial enumeration', 'search1':'exact selected directory, sort=0/order=0/max=1'},
    evidence=['0x2011cfa0 li r5,0 -> SetInt', '0x2011f514 li r5,1 -> SetInt']),
 'getSaveDataStat': entry('libsysutil:0x2011cf00',
    {'elmUtilType':I,'elmUtilOption':I,'elmUserId':I,'elmDirName':S,'elmFileListNumMax':I},
    packets=['resGetSaveDataStat(elmResult,elmIsNewData)',
             'if existing and fileListMax>0: raw 56*fileListNum bytes, possibly zero length end marker',
             'resGetSaveDataStat(elmDataStat:file[1704])'],
    parsers=['0x2011d38c','0x2011d404','0x2011d620','0x2011d4d4']),
 'openSaveList': entry('libsysutil:0x2011f36c',
    {'elmUtilType':I,'elmUtilOption':I,'elmUserId':I,'elmDataListSet':24,'elmContainer':4},
    {'elmFocusDirName':dict(type=S,when='ListSet.focusDirName != 0'),
     'elmNewData':dict(type=16,when='ListSet.newData != 0'),
     'elmDirName':dict(type=S,when='newData != 0 and newData.dirName != 0'),
     'elmNewDataIcon':dict(type=16,when='newData != 0 and newData.icon != 0'),
     'elmNewDataIconTitleName':dict(type=S,when='icon != 0 and icon.title != 0')},
    packets=['resOpenSaveList(elmResult) acceptance', 'guest->VSH raw 48*fixedListNum, only when >0',
             'guest->VSH raw icon bytes, only when iconBufSize>0',
             'resOpenSaveList(elmResult,elmMrID,elmDirName) user selection'],
    evidence=['0x2011f3ec -> 0x2011f43c -> 0x2011f4c8 -> 0x2011f548 -> 0x2011f5b0 -> 0x2011f628',
              'NO elmSearchType/elmSortType/elmSortOrder in this branch', '0x2011fbb8 fixed list; 0x2011fc00 icon']),
 'openSaveData': entry('libsysutil:0x2011dc7c',
    {'elmUtilType':I,'elmUserId':I,'elmDirName':S,'elmDataStat':12,'elmProgress':I},
    {'elmSysFileParam':dict(type=1552,when='StatSet.setParam != 0'),
     'elmIndicator':dict(type=24,when='StatSet.indicator != 0'),
     'elmMsg':dict(type=S,when='indicator != 0 and indicator.msg != 0')},
    packets=['guest->VSH raw indicator picture if indicator.picBufSize>0, BEFORE acceptance',
             'resOpenSaveData(elmResult)'],parsers=['0x2011e3c4..e4b8','0x2011e470']),
 'abort':entry('libsysutil:0x20120230',{'elmAbortParam':64},
    {'elmInvalidMsg':dict(type=S,when='AbortParam.invalidMsg != 0')},
    packets=['resAbort(elmResult) after commit/rollback and requested dialog completion']),
 'openDeleteList':entry('libsysutil:0x201206e4',
    {'elmUtilType':I,'elmUtilOption':I,'elmContainer':4,'elmUserId':I},
    {'elmDirName':dict(type=S,when='helper r7 dirName != 0')},
    packets=['mode8: resOpenDeleteList(elmResult) after user interaction',
             'mode14: resOpenDeleteList(elmResult,elmDataGet:file[108]); caller then sends raw BE32 status'],
    implementation='mode8 implemented; mode14 explicit unsupported trap'),
 'getListItem':entry('savedata:0x30002044',{'elmUserId':I,'elmDirName':S},
    packets=['resGetListItem(elmResult,elmListItem:file[1616])'],
    layout={'dirStat': [0,56], 'sysFileParam':[56,1552], 'bind':[1608,4],'sizeKB':[1612,4]},
    parsers=['0x300021e0 length=0x650','0x30002228..22d0']),
 'releaseOwnerShip':entry('savedata:0x30002808',{'elmUtilType':I,'elmUtilOption':I},
    packets=['resReleaseOwnerShip(elmResult)']),
 'showDialog':entry('savedata:0x30001cac',{'elmDialogParam':28},
    {'elmInvalidMsg':dict(type=S,when='DialogParam.invalidMsg != 0')},
    packets=['resShowDialog(elmResult)'],implementation='explicit unsupported trap'),
 'execPlugin':entry('savedata:0x30000c24,0x30001598',
    {'elmUtilType':I,'elmUtilOption':I,'elmUserId':I},
    {'elmDataListSet':dict(type=24,when='mode13 list delete'),
     'elmFocusDirName':dict(type=S,when='mode13 and focusDirName != 0'),
     'elmSetList':dict(type=16,when='setList != 0 in import/export branch'),
     'elmDirNamePlefix':dict(type=S,when='setList != 0 and dirNamePrefix != 0'),
     'elmDirName':dict(type=S,when='fixed import/export dirName != 0'),
     'elmMaxSizeKb':dict(type=I,when='import/export branch')},
    packets=['resExecPlugin(elmResult) acceptance','guest->VSH raw fixed rows for mode13',
             'resPluginResult(elmResult,elmDataGet:file[108])','guest->VSH raw BE32 callback status; repeated'],
    implementation='explicit unsupported trap; external storage/account protocol not implemented'),
}
for name in ('readData','writeData','deleteData'):
    # elmProgress follows elmFileName unconditionally (SetInt 0x2011e988..e990, name in r19 from 0x2011e300).
    PROTOCOL[name]=entry('libsysutil:0x2011e8bc..ec98',
       {'elmDataFileSet':48,'elmFileType':I,'elmFileName':S,'elmProgress':I},
       packets=({'readData':['resReadData(elmResult,elmDataSize)','raw bytes if excSize>0'],
                 'writeData':['resWriteData(elmResult) acceptance','guest raw bytes (unless size0)',
                              'resWriteData(elmResult,elmDataSize) completion'],
                 'deleteData':['resDeleteData(elmResult,elmDataSize)']}[name]))


def validation_cpp():
    """Generate strict structural validation from the same catalogue as report."""
    result=['static void validate_protocol(const Document& q){']
    for name, spec in PROTOCOL.items():
        result.append(f'if(q.name=="{name}")'+'{')
        required=','.join('"'+x+'"' for x in spec['required'])
        optional=','.join('"'+x+'"' for x in spec['conditional'])
        result.append('q.shape({'+required+'},{'+optional+'});')
        fields=dict(spec['required']);fields.update({k:v['type'] for k,v in spec['conditional'].items()})
        for key, typ in fields.items():
            expr=(f'q.blob("{key}",{typ})' if isinstance(typ,int) else
                  f'q.integer("{key}")' if typ==I else f'q.string("{key}")')
            result.append(f'if(q.attrs.count("{key}"))(void){expr};')
        result.append('return;}')
    result.append('throw std::runtime_error("unsupported SAVE command: "+q.name);}')
    return '\n'.join(result)
