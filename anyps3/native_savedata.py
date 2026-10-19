"""Host VSH SAVE service; original firmware retains all game callback calls.

Wire evidence: libsysutil 3.01 0x2011c988..0x20120ecc, VSH SAVE
0x12d700..0x1386b4 (see local/ratchet/codex/savedata). Interactive lists
use native_vsh_ui; secure/PFD support is not advertised.
"""
from .native_savedata_protocol import validation_cpp

TYPES = r'''
#include <filesystem>
#include <map>
#include <sys/syscall.h>
#include <linux/fs.h>
#include <system_error>
struct NativeSavedata {
    using Bytes=std::vector<std::uint8_t>;
    struct Packet { Bytes bytes; std::size_t sent=0; };
    std::vector<Packet> replies;
    std::uint64_t owner=0;
    std::uint32_t mode=0,options=0,user=1;
    bool modules=false,opened=false,writing=false,failed=false;
    bool selected_list=false;
    std::uint64_t retired_owner=0;
    std::string directory,file;
    std::uint32_t operation=0,file_offset=0,file_size=0;
    Bytes incoming;
    Bytes secure_id;
    std::filesystem::path staging;
    // The runtime's configured local user is 1; 0 means current user.
    // This is separate from existence of any savedata directory.
};
'''
CPP = r'''
static void savedata_ui_open_list(PpuState&,const std::vector<std::uint8_t>&);
static void savedata_ui_error(PpuState&,const std::vector<std::uint8_t>&);
static void savedata_ui_delete(PpuState&,const std::vector<std::uint8_t>&);
static void savedata_ui_indicator(PpuState&,const std::vector<std::uint8_t>&);
static void savedata_ui_close(PpuState&);
namespace savedata_vsh {
using Bytes = NativeSavedata::Bytes;
static std::uint32_t be(const Bytes &b, std::size_t a) {
    if (a > b.size() || b.size() - a < 4)
        throw std::runtime_error("SAVE truncated BE word");
    return (std::uint32_t(b[a]) << 24) | (std::uint32_t(b[a + 1]) << 16) | (std::uint32_t(b[a + 2]) << 8) |
           b[a + 3];
}
static void put(Bytes &b, std::size_t a, std::uint64_t v, unsigned n = 4) {
    if (a > b.size() || n > b.size() - a)
        throw std::runtime_error("SAVE output bounds");
    for (unsigned j = 0; j < n; ++j)
        b[a + j] = v >> ((n - j - 1) * 8);
}
static std::string text(const Bytes &b, std::size_t a, std::size_t n) {
    if (a > b.size() || n > b.size() - a)
        throw std::runtime_error("SAVE string bounds");
    const auto end = std::find(b.begin() + a, b.begin() + a + n, 0);
    if (end == b.begin() + a + n)
        throw std::runtime_error("SAVE unterminated string");
    return std::string(b.begin() + a, end);
}
struct Attr {
    std::uint32_t type = 0, value = 0;
    Bytes data;
    std::string string;
};
struct Document {
    std::string name;
    std::map<std::string, Attr> attrs;
    explicit Document(const Bytes &b, std::uint32_t tag=0x53415645, bool floats=false) {
        if (b.size() < 92 || be(b, 0) != tag || be(b, 4) != 0x110 || be(b, 8) != 64)
            throw std::runtime_error("invalid SAVE CXML header");
        const auto tree = be(b, 12), strings = be(b, 24), ns = be(b, 28), binary = be(b, 48), nb = be(b, 52),
                   slots = be(b, 68);
        if (slots > 64 || tree != 28 + 16 * slots || tree > b.size() - 64 || strings < 64 + tree ||
            strings > b.size() || ns > b.size() - strings || binary < strings + ns || binary > b.size() ||
            nb > b.size() - binary)
            throw std::runtime_error("SAVE CXML table bounds");
        for (unsigned a = 72; a < 92; a += 4)
            if (be(b, a) != UINT32_MAX)
                throw std::runtime_error("SAVE nested request unsupported");
        auto str = [&](unsigned at) {
            if (at >= ns)
                throw std::runtime_error("SAVE string offset");
            return text(b, strings + at, ns - at);
        };
        name = str(be(b, 64));
        for (unsigned i = 0; i < slots; ++i) {
            const auto a = 92 + 16 * i, key = be(b, a);
            if (key == UINT32_MAX)
                continue;
            const auto type = be(b, a + 4), v = be(b, a + 8), n = be(b, a + 12);
            Attr attr;
            attr.type = type;
            attr.value = v;
            if (type == 1 || (floats && type == 2)) {
                if (n)
                    throw std::runtime_error("SAVE integer padding");
            } else if (type == 3) {
                attr.string = str(v);
                if (attr.string.size() != n)
                    throw std::runtime_error("SAVE string length");
            } else if (type == 6) {
                if (v > nb || n > nb - v)
                    throw std::runtime_error("SAVE blob bounds");
                attr.data.assign(b.begin() + binary + v, b.begin() + binary + v + n);
            } else
                throw std::runtime_error("SAVE unsupported attribute type");
            if (!attrs.emplace(str(key), std::move(attr)).second)
                throw std::runtime_error("SAVE duplicate attribute");
        }
    }
    const Attr &attr(const std::string &key, unsigned type) const {
        const auto it = attrs.find(key);
        if (it == attrs.end() || it->second.type != type)
            throw std::runtime_error("SAVE missing/wrong attribute: " + key);
        return it->second;
    }
    unsigned integer(const std::string &key) const { return attr(key, 1).value; }
    std::string string(const std::string &key) const { return attr(key, 3).string; }
    Bytes blob(const std::string &key, std::size_t n) const {
        const auto &b = attr(key, 6).data;
        if (b.size() != n)
            throw std::runtime_error("SAVE blob size: " + key);
        return b;
    }
    void shape(std::initializer_list<const char *> required,
               std::initializer_list<const char *> optional = {}) const {
        for (const auto *key : required)
            if (!attrs.count(key))
                throw std::runtime_error("SAVE " + name + " missing attribute: " + std::string(key));
        for (const auto &kv : attrs) {
            bool found = false;
            for (const auto *key : required)
                found |= kv.first == key;
            for (const auto *key : optional)
                found |= kv.first == key;
            if (!found)
                throw std::runtime_error("SAVE unknown attribute: " + kv.first);
        }
    }
};
''' + validation_cpp() + r'''
static Bytes reply(const std::string &root, std::initializer_list<std::pair<std::string, std::uint32_t>> ints,
                   const std::string &blob_name = "", const Bytes &blob = {},
                   const std::string &string_name = "", const std::string &string_value = "") {
    std::string names = root;
    names.push_back(0);
    std::vector<unsigned> keys;
    for (const auto &kv : ints) {
        keys.push_back(names.size());
        names += kv.first;
        names.push_back(0);
    }
    const unsigned bk = names.size();
    if (!blob_name.empty()) {
        names += blob_name;
        names.push_back(0);
    }
    const unsigned sk=names.size();
    if(!string_name.empty()){names+=string_name;names.push_back(0);}
    const unsigned sv=names.size();
    if(!string_name.empty()){names+=string_value;names.push_back(0);}
    const unsigned count = ints.size() + !blob_name.empty() + !string_name.empty(), tree = 28 + 16 * count,
                   strings = (64 + tree + 15) & ~15u, binary = (strings + names.size() + 15) & ~15u;
    Bytes b(binary + ((blob.size() + 15) & ~std::size_t(15)), 0);
    put(b, 0, 0x53415645);
    put(b, 4, 0x110);
    put(b, 8, 64);
    put(b, 12, tree);
    put(b, 16, strings);
    put(b, 24, strings);
    put(b, 28, names.size());
    put(b, 32, binary);
    put(b, 40, binary);
    put(b, 48, binary);
    put(b, 52, blob.size());
    put(b, 68, count);
    for (unsigned a = 72; a < 92; a += 4)
        put(b, a, UINT32_MAX);
    unsigned i = 0;
    for (const auto &kv : ints) {
        put(b, 92 + i * 16, keys[i]);
        put(b, 96 + i * 16, 1);
        put(b, 100 + i * 16, kv.second);
        ++i;
    }
    if (!blob_name.empty()) {
        put(b, 92 + i * 16, bk);
        put(b, 96 + i * 16, 6);
        put(b, 104 + i * 16, blob.size());
        ++i;
    }
    if(!string_name.empty()){
        put(b,92+i*16,sk);put(b,96+i*16,3);put(b,100+i*16,sv);put(b,104+i*16,string_value.size());
    }
    std::copy(names.begin(), names.end(), b.begin() + strings);
    std::copy(blob.begin(), blob.end(), b.begin() + binary);
    return b;
}
static void enqueue(NativeSavedata &d, Bytes b) {
    d.replies.push_back({std::move(b), 0});
}
static void result(NativeSavedata &d, const std::string &root, unsigned code = 0) {
    enqueue(d, reply(root, {{"elmResult", code}}));
}
static void component(const std::string &name, unsigned max, bool file = false) {
    if (name.empty() || name.size() > max || name == "." || name == ".." ||
        name.find_first_not_of(file ? "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
                                    : "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-") !=
            std::string::npos)
        throw std::runtime_error("SAVE invalid path component: " + name);
}
static std::filesystem::path root(PpuState &s) {
    if (s.fs.hdd_root.empty())
        throw std::runtime_error("SAVE requires ANYPS3_HDD_ROOT");
    try {
        char home[64];std::snprintf(home,sizeof(home),"/dev_hdd0/home/%08u/savedata",s.settings.savedata.user);
        return s.fs.resolve(home).host;
    } catch (const NativeFilesystem::Error &e) {
        throw std::runtime_error("SAVE rejected HDD path: " + std::to_string(e.code));
    }
}
static std::filesystem::path path(PpuState &s, const std::string &dir, const std::string &file = "") {
    component(dir, 31);
    if (!file.empty())
        component(file, 12, true);
    try {
        char home[64];std::snprintf(home,sizeof(home),"/dev_hdd0/home/%08u/savedata/",s.settings.savedata.user);
        return s.fs.resolve(std::string(home) + dir + (file.empty() ? "" : "/" + file))
            .host;
    } catch (const NativeFilesystem::Error &e) {
        throw std::runtime_error("SAVE rejected HDD path: " + std::to_string(e.code));
    }
}
static Bytes read(const std::filesystem::path &p, std::size_t limit = 1048576) {
    std::ifstream f(p, std::ios::binary | std::ios::ate);
    if (!f)
        throw std::system_error(errno?errno:EIO,std::generic_category(),"SAVE file open: " + p.string());
    const auto n = f.tellg();
    if (n < 0 || std::uint64_t(n) > limit)
        throw std::runtime_error("SAVE file size exceeds supported bound");
    Bytes b(static_cast<std::size_t>(n));
    f.seekg(0);
    if (!b.empty() && !f.read(reinterpret_cast<char *>(b.data()), b.size()))
        throw std::system_error(errno?errno:EIO,std::generic_category(),"SAVE file read");
    return b;
}
struct NoUser:std::runtime_error{using std::runtime_error::runtime_error;};
struct Corrupt:std::runtime_error{using std::runtime_error::runtime_error;};
struct Sfo {
    struct Value {
        unsigned format;
        Bytes bytes;
        unsigned capacity;
    };
    std::map<std::string, Value> values;
    explicit Sfo(const Bytes &b) {
        auto le = [&](std::size_t a, unsigned n = 4) {
            if (a > b.size() || n > b.size() - a)
                throw Corrupt("SAVE truncated SFO");
            unsigned v = 0;
            for (unsigned j = 0; j < n; ++j)
                v |= unsigned(b[a + j]) << (8 * j);
            return v;
        };
        if (le(0) != 0x46535000 || le(4) != 0x101)
            throw Corrupt("SAVE corrupt PARAM.SFO header");
        const auto keys = le(8), data = le(12), count = le(16);
        if (count > 128 || 20 + count * 16 > keys || keys > data || data > b.size())
            throw Corrupt("SAVE corrupt SFO tables");
        for (unsigned i = 0; i < count; ++i) {
            const auto a = 20 + 16 * i, k = le(a, 2), fmt = le(a + 2, 2), n = le(a + 4), cap = le(a + 8),
                       off = le(a + 12);
            if (k >= data - keys || off > b.size() - data || cap > b.size() - data - off || n > cap)
                throw Corrupt("SAVE corrupt SFO range");
            const auto name = text(b, keys + k, data - keys - k);
            if (name.empty())
                throw Corrupt("SAVE empty SFO key");
            Bytes v(b.begin() + data + off, b.begin() + data + off + n);
            if ((fmt == 0x204 && (v.empty() || v.back())) || (fmt == 0x404 && (n != 4 || cap != 4)) ||
                (fmt != 0x204 && fmt != 0x404 && fmt != 4))
                throw Corrupt("SAVE corrupt SFO value");
            if (!values.emplace(name, Value{fmt, std::move(v), cap}).second)
                throw Corrupt("SAVE duplicate SFO key");
        }
    }
    Sfo() = default;
    std::string string(const std::string &key, unsigned max) const {
        const auto it = values.find(key);
        if (it == values.end() || it->second.format != 0x204 || it->second.bytes.size() > max)
            throw Corrupt("SAVE SFO missing/invalid " + key);
        return text(it->second.bytes, 0, it->second.bytes.size());
    }
    unsigned integer(const std::string &key) const {
        const auto it = values.find(key);
        if (it == values.end() || it->second.format != 0x404)
            throw Corrupt("SAVE SFO integer " + key);
        const auto &b = it->second.bytes;
        return unsigned(b[0]) | (unsigned(b[1]) << 8) | (unsigned(b[2]) << 16) | (unsigned(b[3]) << 24);
    }
    void string(const std::string &key, const std::string &v, unsigned cap) {
        if (v.size() >= cap)
            throw std::runtime_error("SAVE SFO string too long");
        Bytes b(v.begin(), v.end());
        b.push_back(0);
        values[key] = {0x204, std::move(b), cap};
    }
    void integer(const std::string &key, unsigned v) {
        Bytes b(4);
        for (unsigned j = 0; j < 4; ++j)
            b[j] = v >> (j * 8);
        values[key] = {0x404, std::move(b), 4};
    }
    Bytes encode() const {
        unsigned ksize = 0, vsize = 0;
        for (const auto &kv : values) {
            ksize += kv.first.size() + 1;
            vsize += (kv.second.capacity + 3) & ~3u;
        }
        const unsigned keys = 20 + 16 * values.size(), data = (keys + ksize + 3) & ~3u;
        Bytes b(data + vsize, 0);
        auto le = [&](unsigned a, unsigned v, unsigned n = 4) {
            for (unsigned j = 0; j < n; ++j)
                b[a + j] = v >> (j * 8);
        };
        le(0, 0x46535000);
        le(4, 0x101);
        le(8, keys);
        le(12, data);
        le(16, values.size());
        unsigned i = 0, k = 0, v = 0;
        for (const auto &kv : values) {
            const auto a = 20 + 16 * i++;
            le(a, k, 2);
            le(a + 2, kv.second.format, 2);
            le(a + 4, kv.second.bytes.size());
            le(a + 8, kv.second.capacity);
            le(a + 12, v);
            std::copy(kv.first.begin(), kv.first.end(), b.begin() + keys + k);
            std::copy(kv.second.bytes.begin(), kv.second.bytes.end(), b.begin() + data + v);
            k += kv.first.size() + 1;
            v += (kv.second.capacity + 3) & ~3u;
        }
        return b;
    }
};
static Sfo metadata(PpuState &s, const std::string &dir) {
    if(!std::filesystem::exists(path(s,dir,"PARAM.SFO")))throw Corrupt("SAVE directory without PARAM.SFO");
    auto f = Sfo(read(path(s, dir, "PARAM.SFO")));
    if (f.string("SAVEDATA_DIRECTORY", 32) != dir)
        throw Corrupt("SAVE SFO directory mismatch");
    // Authenticated console saves require account/PFD verification, not a
    // fabricated bind=0. This backend only supports its unprotected host saves.
    if (std::filesystem::exists(path(s, dir, "PARAM.PFD")))
        throw std::runtime_error("SAVE authenticated PARAM.PFD/account binding unsupported");
    // RPCS3 stores plaintext with an all-zero account and empty PARAMS.
    // Nonempty binding data cannot be authenticated by this backend.
    for(const char* key:{"ACCOUNT_ID","PARAMS","PARAMS2"}){
        const auto it=f.values.find(key);if(it==f.values.end())continue;
        for(auto c:it->second.bytes)if(c && !(std::string(key)=="ACCOUNT_ID" && c=='0'))
            throw std::runtime_error("SAVE authenticated SFO binding unsupported: "+std::string(key));
    }
    return f;
}
static unsigned filetype(const std::string &name) {
    if (name == "ICON0.PNG")
        return 2;
    if (name == "ICON1.PAM")
        return 3;
    if (name == "PIC1.PNG")
        return 4;
    if (name == "SND0.AT3")
        return 5;
    return 1;
}
static std::vector<std::filesystem::path> files(PpuState &s, const std::string &dir) {
    std::vector<std::filesystem::path> out;
    for (const auto &e : std::filesystem::directory_iterator(path(s, dir))) {
        const auto n = e.path().filename().string();
        const auto p = path(s, dir, n);
        if (!std::filesystem::is_regular_file(p))
            throw std::runtime_error("SAVE nonregular file");
        if (n != "PARAM.SFO" && n != "PARAM.PFD")
            out.push_back(p);
    }
    std::sort(out.begin(), out.end());
    return out;
}
static void times(Bytes &b, unsigned a, const std::filesystem::path &p) {
    struct stat st{};
    if (::stat(p.c_str(), &st))
        throw std::runtime_error("SAVE host stat failed");
    put(b, a, std::uint64_t(st.st_atime), 8);
    put(b, a + 8, std::uint64_t(st.st_mtime), 8);
    put(b, a + 16, std::uint64_t(st.st_ctime), 8);
}
static void user(PpuState &s, unsigned id) {
    auto& d=s.settings.savedata;
    if (id > 99999999)throw std::runtime_error("SAVE invalid user account");
    const auto selected=id?id:1;
    if(selected!=1){
        char home[64];std::snprintf(home,sizeof(home),"/dev_hdd0/home/%08u",selected);
        if(!std::filesystem::is_directory(s.fs.resolve(home).host))throw NoUser("SAVE user account does not exist");
    }
    if(d.opened && d.user!=selected)throw std::runtime_error("SAVE user changed while open");
    d.user=selected;
}
static void mode(const NativeSavedata &d, const Document &q) {
    if (q.integer("elmUtilType") != d.mode)
        throw std::runtime_error("SAVE mode changed during session");
}
static void write_file(const std::filesystem::path &p, const Bytes &b) {
    const int fd = ::open(p.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
    if (fd < 0)
        throw std::system_error(errno,std::generic_category(),"SAVE host write open failed");
    std::size_t at = 0;
    while (at < b.size()) {
        const auto n = ::write(fd, b.data() + at, b.size() - at);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0) {
            const int error=errno?errno:EIO;::close(fd);
            throw std::system_error(error,std::generic_category(),"SAVE host write failed");
        }
        at += n;
    }
    const int sync=::fsync(fd),sync_errno=errno,closed=::close(fd);
    if(sync || closed)throw std::system_error(sync?sync_errno:errno,std::generic_category(),"SAVE host flush failed");
}
static void cleanup(NativeSavedata &d) {
    if(d.owner && d.modules)d.retired_owner=d.owner;
    if (!d.staging.empty())
        std::filesystem::remove_all(d.staging);
    d.staging.clear();
    d.owner = 0;
    d.modules = false;
    d.opened = false;
    d.writing = false;
    d.failed = false;
    d.directory.clear();
    d.file.clear();
    d.incoming.clear();
    d.secure_id.clear();d.selected_list=false;
}
static void commit(PpuState &s, NativeSavedata &d) {
    if (d.staging.empty())
        return;
    const auto dest = path(s, d.directory);
    const int stage_fd = ::open(d.staging.c_str(), O_RDONLY | O_DIRECTORY);
    if (stage_fd < 0)
        throw std::system_error(errno,std::generic_category(),"SAVE staging directory open failed");
    const int stage_sync = ::fsync(stage_fd), stage_close = ::close(stage_fd);
    if (stage_sync || stage_close)
        throw std::system_error(errno,std::generic_category(),"SAVE staging directory flush failed");
    // Use Linux atomic directory exchange for existing data; never remove the
    // only old copy before the complete replacement is durably written.
    if (std::filesystem::exists(dest)) {
        if (::syscall(SYS_renameat2, AT_FDCWD, d.staging.c_str(), AT_FDCWD, dest.c_str(), RENAME_EXCHANGE))
            throw std::system_error(errno,std::generic_category(),"SAVE atomic directory exchange failed");
    } else
        std::filesystem::rename(d.staging, dest);
    const int fd = ::open(dest.parent_path().c_str(), O_RDONLY | O_DIRECTORY);
    if (fd < 0)
        throw std::system_error(errno,std::generic_category(),"SAVE parent directory open failed");
    const int synced = ::fsync(fd), closed = ::close(fd);
    if (synced || closed)
        throw std::system_error(errno,std::generic_category(),"SAVE parent directory flush failed");
}
static void finish_write(NativeSavedata &d) {
    if (d.incoming.size() != d.file_size)
        throw std::runtime_error("SAVE incomplete write packet");
    const auto p = d.staging / d.file;
    Bytes data;
    if (std::filesystem::exists(p))
        data = read(p, 64 * 1024 * 1024);
    const auto end = std::uint64_t(d.file_offset) + d.file_size;
    if (end > 64 * 1024 * 1024)
        throw std::runtime_error("SAVE file exceeds backend bound");
    if (d.operation == 1)
        data.resize(end);
    else if (data.size() < end)
        data.resize(end);
    std::copy(d.incoming.begin(), d.incoming.end(), data.begin() + d.file_offset);
    write_file(p, data);
    if(std::filesystem::exists(d.staging/"PARAM.SFO")){
        auto f=Sfo(read(d.staging/"PARAM.SFO"));
        if(!d.secure_id.empty()){
        f.integer("*"+d.file,1);
        f.values["ANYPS3_SECURE_"+d.file]={4,d.secure_id,16};
        }else{f.values.erase("*"+d.file);f.values.erase("ANYPS3_SECURE_"+d.file);}
        write_file(d.staging/"PARAM.SFO",f.encode());
    }
    d.writing = false;
    d.incoming.clear();
    enqueue(d, reply("resWriteData", {{"elmResult", 0}, {"elmDataSize", d.file_size}}));
}
static void raw_write(NativeSavedata &d, const Bytes &b, bool end) {
    if (!d.writing || b.size() > d.file_size - d.incoming.size())
        throw std::runtime_error("SAVE unexpected/oversized raw write");
    d.incoming.insert(d.incoming.end(), b.begin(), b.end());
    if (end){
        try{finish_write(d);}catch(const std::system_error& e){
            d.failed=true;d.writing=false;d.incoming.clear();
            result(d,"resWriteData",e.code().value()==ENOSPC || e.code().value()==EDQUOT?0x80029202u:0x80029201u);
        }
    }
    else if (d.incoming.size() == d.file_size)
        throw std::runtime_error("SAVE missing write end flag");
}
static void request_impl(PpuState &s, const Bytes &bytes) {
    auto &d = s.settings.savedata;
    if(s.settings.vsh_ui.open && s.settings.vsh_ui.kind!=NativeVshUi::Message && s.settings.vsh_ui.kind!=NativeVshUi::SaveIndicator)
        throw std::runtime_error("SAVE command while interactive response is pending");
    Document q(bytes);
    if(std::getenv("ANYPS3_SYSUTIL_TRACE")){
        std::fprintf(stderr,"native SAVE request name=%s mode=%u user=%u attributes=",q.name.c_str(),d.mode,d.user);
        for(const auto& a:q.attrs)std::fprintf(stderr," %s:%u",a.first.c_str(),a.second.type);
        std::fprintf(stderr,"\n");
    }
    validate_protocol(q);
    // Delete2 reaches openDeleteList directly (libsysutil 0x20120014..003c),
    // without the common getOwnerShip/loadModules sequence.
    if (q.name == "openDeleteList"){savedata_ui_delete(s,bytes);return;}

    if(q.name=="getListItem"){
        q.shape({"elmUserId","elmDirName"});
        if(d.owner && d.owner!=s.thread_id)throw std::runtime_error("SAVE getListItem while busy");
        user(s,q.integer("elmUserId"));const auto dir=q.string("elmDirName"),p=path(s,dir).string();
        if(!std::filesystem::exists(p)){result(d,"resGetListItem",0x80029262);return;}
        const auto f=metadata(s,dir);Bytes item(1616,0);times(item,0,p);
        std::copy(dir.begin(),dir.end(),item.begin()+24);
        for(const auto& field:std::vector<std::pair<const char*,unsigned>>{{"TITLE",0},{"SUB_TITLE",128},{"DETAIL",256},{"SAVEDATA_LIST_PARAM",1288}}){
            const auto value=f.string(field.first,field.second==256?1024:field.second==1288?8:128);
            std::copy(value.begin(),value.end(),item.begin()+56+field.second);
        }
        put(item,56+1280,f.integer("ATTRIBUTE"));put(item,56+1284,f.integer("PARENTAL_LEVEL"));
        std::uint64_t kb=35;for(const auto& file:files(s,dir))kb+=(std::filesystem::file_size(file)+1023)/1024;
        put(item,1612,kb);
        enqueue(d,reply("resGetListItem",{{"elmResult",0}},"elmListItem",item));return;
    }
    if(q.name=="releaseOwnerShip"){
        q.shape({"elmUtilType","elmUtilOption"});mode(d,q);
        if(!d.owner || d.owner!=s.thread_id || d.opened)throw std::runtime_error("SAVE invalid release ownership");
        cleanup(d);result(d,"resReleaseOwnerShip");return;
    }
    if (q.name == "getOwnerShip") {
        q.shape({"elmUtilType", "elmUtilOption"});
        const auto m = q.integer("elmUtilType"), o = q.integer("elmUtilOption");
        if (m > 5 && m != 14)
            throw std::runtime_error("SAVE unsupported operation mode");
        if (d.owner)throw std::runtime_error("SAVE ownership already held");
        d.user=1;d.failed=false;d.retired_owner=0;
        (void)root(s);
        d.owner = s.thread_id;
        d.mode = m;
        d.options = o;
        result(d, "resGetOwnerShip");
        return;
    }
    // Original common exit: abort reply -> unloadModules(type14), at
    // 3.01 libsysutil 0x2011f19c..f1b0, helper 0x20120b50. The I/O
    // transaction has ended; its retired VSH context still needs unloading.
    if(q.name=="unloadModules" && !d.owner && d.retired_owner==s.thread_id){
        q.shape({"elmUtilType"});
        if(q.integer("elmUtilType")!=14)throw std::runtime_error("SAVE invalid terminal unload type");
        d.retired_owner=0;result(d,"resUnloadModules");return;
    }
    if (!d.owner || d.owner != s.thread_id)
        throw std::runtime_error("SAVE request outside owning PPU session");
    if (q.name == "loadModules") {
        q.shape({"elmUtilType", "elmContainer"});
        mode(d, q);
        (void)q.blob("elmContainer", 4);
        if (d.modules)
            throw std::runtime_error("SAVE modules already loaded");
        // Load the host implementation of the VSH service. No guest PRX is
        // claimed to be loaded; loadModules is a VSH-side operation.
        d.modules = true;
        result(d, "resLoadModules");
        return;
    }
    if (q.name == "unloadModules") {
        q.shape({"elmUtilType"});
        mode(d, q);
        if (!d.modules || d.opened)
            throw std::runtime_error("SAVE invalid unloadModules state");
        d.modules = false;
        result(d, "resUnloadModules");
        return;
    }
    if (q.name == "abort") {
        q.shape({"elmAbortParam"}, {"elmInvalidMsg"});
        const auto a = q.blob("elmAbortParam", 64);
        // +8 = public API result; +12 = signed callback result. The guest
        // still owns error mapping. An error dialog needs a real UI response.
        const auto api = be(a, 8);
        const auto cb = static_cast<std::int32_t>(be(a, 12));
        if (api && be(a, 24)) {
            savedata_ui_error(s,bytes);return;
        }
        if (!api && cb >= 0 && !d.failed)
            commit(s, d);
        savedata_ui_close(s);
        cleanup(d);
        result(d, "resAbort");
        return;
    }
    if (!d.modules)
        throw std::runtime_error("SAVE service not loaded");
    if(q.name=="openSaveList"){
        savedata_ui_open_list(s,bytes);return;
    }
    if (q.name == "getDirList") {
        q.shape({"elmUserId", "elmSearchType", "elmSortType", "elmSortOrder", "elmDirNamePlefix",
                 "elmDirListNumMax"});
        user(s, q.integer("elmUserId"));
        const auto search=q.integer("elmSearchType");
        if (d.opened || search>1 || q.integer("elmSortType") > 1 ||
            q.integer("elmSortOrder") > 1 || q.integer("elmDirListNumMax") > 2048)
            throw std::runtime_error("SAVE unsupported directory search");
        const auto prefix = q.string("elmDirNamePlefix");
        if (prefix.size() > 255 ||
            prefix.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-|") !=
                std::string::npos)
            throw std::runtime_error("SAVE unsupported prefix expression");
        struct Entry {
            std::string name, list, title;
            std::int64_t mtime;
        };
        std::vector<Entry> entries;
        const auto parent = root(s);
        if (std::filesystem::exists(parent))
            for (const auto &e : std::filesystem::directory_iterator(parent)) {
                const auto n = e.path().filename().string();
                bool matches=false;
                for(std::size_t a=0;a<=prefix.size();){
                    const auto sep=prefix.find('|',a),end=sep==std::string::npos?prefix.size():sep;
                    const auto part=prefix.substr(a,end-a);
                    matches|=search?n==part:n.compare(0,part.size(),part)==0;
                    if(sep==std::string::npos)break;
                    a=sep+1;
                }
                if(!matches)continue;
                // Private staging directories are not completed guest saves.
                if (n.rfind(".anyps3-save-", 0) == 0)
                    continue;
                const auto p = path(s, n);
                if (!std::filesystem::is_directory(p))
                    throw std::runtime_error("SAVE unexpected directory entry");
                auto f = metadata(s, n);
                struct stat st{};
                if (::stat(p.c_str(), &st))
                    throw std::runtime_error("SAVE directory stat failed");
                entries.push_back(
                    {n, f.string("SAVEDATA_LIST_PARAM", 8), f.string("SUB_TITLE", 128), st.st_mtime});
            }
        const auto sort = q.integer("elmSortType"), order = q.integer("elmSortOrder");
        std::sort(entries.begin(), entries.end(), [&](const auto &a, const auto &b) {
            if (!sort && a.mtime != b.mtime)
                return order ? a.mtime < b.mtime : a.mtime > b.mtime;
            if (sort && a.title != b.title)
                return order ? a.title < b.title : a.title > b.title;
            return a.name < b.name;
        });
        const auto count = std::min<std::size_t>(entries.size(), q.integer("elmDirListNumMax"));
        Bytes list(count * 48, 0);
        for (unsigned i = 0; i < count; ++i) {
            std::copy(entries[i].name.begin(), entries[i].name.end(), list.begin() + 48 * i);
            std::copy(entries[i].list.begin(), entries[i].list.end(), list.begin() + 48 * i + 32);
        }
        enqueue(d, reply("resGetDirList", {{"elmResult", 0},
                                           {"elmDirNum", static_cast<unsigned>(entries.size())},
                                           {"elmListedDirNum", static_cast<unsigned>(count)}}));
        if (count)
            enqueue(d, std::move(list));
        return;
    }
    if (q.name == "getSaveDataStat") {
        q.shape({"elmUtilType", "elmUtilOption", "elmUserId", "elmDirName", "elmFileListNumMax"});
        mode(d, q);
        user(s, q.integer("elmUserId"));
        if (d.opened || q.integer("elmFileListNumMax") > 2048)
            throw std::runtime_error("SAVE invalid stat state/limit");
        const auto dir = q.string("elmDirName");
        const auto p = path(s, dir);
        const bool exists = std::filesystem::exists(p);
        Bytes stat(0x6a8, 0), list;
        put(stat, 0,
            std::min<std::uint64_t>(INT32_MAX, std::filesystem::space(s.fs.hdd_root).available / 1024));
        put(stat, 4, !exists);
        // Firmware 3.01 ps3_savedata_plugin_game_mini, VA 0xfc4c..fc5c:
        // li 0,35; stw 0,1624(StatGet), also before the new-data branch.
        put(stat, 1624, 35);
        std::copy(dir.begin(), dir.end(), stat.begin() + 32);
        if (exists) {
            auto f = metadata(s, dir);
            times(stat, 8, p);
            const auto copy = [&](const char *key, unsigned at, unsigned cap) {
                const auto v = f.string(key, cap);
                std::copy(v.begin(), v.end(), stat.begin() + at);
            };
            copy("TITLE", 64, 128);
            copy("SUB_TITLE", 192, 128);
            copy("DETAIL", 320, 1024);
            copy("SAVEDATA_LIST_PARAM", 1352, 8);
            put(stat, 1344, f.integer("ATTRIBUTE"));
            put(stat, 1348, f.integer("PARENTAL_LEVEL"));
            const auto fs = files(s, dir);
            const auto count = std::min<std::size_t>(fs.size(), q.integer("elmFileListNumMax"));
            list.resize(count * 56, 0);
            std::uint64_t content_kb = 0;
            for (unsigned i = 0; i < fs.size(); ++i) {
                const auto size = std::filesystem::file_size(fs[i]);
                content_kb += (size + 1023) / 1024;
                if (i < count) {
                    const auto n = fs[i].filename().string();
                    put(list, 56 * i, f.values.count("*"+n) && f.integer("*"+n)?0:filetype(n));
                    put(list, 56 * i + 8, size, 8);
                    times(list, 56 * i + 16, fs[i]);
                    std::copy(n.begin(), n.end(), list.begin() + 56 * i + 40);
                }
            }
            put(stat, 1620, content_kb + 35);
            put(stat, 1628, fs.size());
            put(stat, 1632, count);
        }
        d.directory = dir;
        enqueue(d, reply("resGetSaveDataStat", {{"elmResult", 0}, {"elmIsNewData", !exists}}));
        // The client performs a raw list read whenever old data and a nonzero
        // limit are present, including a zero-length end-of-packet marker.
        if (exists && q.integer("elmFileListNumMax")) {
            enqueue(d, std::move(list));
        }
        enqueue(d, reply("resGetSaveDataStat", {}, "elmDataStat", stat));
        return;
    }
    if (q.name == "openSaveData") {
        q.shape({"elmUtilType", "elmUserId", "elmDirName", "elmDataStat", "elmProgress"},
                {"elmIndicator", "elmMsg", "elmSysFileParam"});
        mode(d, q);
        user(s, q.integer("elmUserId"));
        const auto dir = q.string("elmDirName");
        const auto target = path(s, dir);
        const auto set = q.blob("elmDataStat", 12);
        if (d.opened || dir != d.directory)
            throw std::runtime_error("SAVE open does not match stat");
        const auto recreate=be(set,4);
        if((recreate&0xfffeffffu)>3 || be(set,8))
            throw std::runtime_error("SAVE invalid StatSet recreate/reserved");
        if(bool(be(set,0))!=bool(q.attrs.count("elmSysFileParam")))
            throw std::runtime_error("SAVE StatSet metadata pointer/attribute mismatch");
        if(q.attrs.count("elmMsg") && !q.attrs.count("elmIndicator"))
            throw std::runtime_error("SAVE indicator message without indicator");
        (void)q.integer("elmProgress");
        if ((d.mode & 1) == 0) {
            if (q.attrs.count("elmSysFileParam")) {
                const auto param = q.blob("elmSysFileParam", 1552);
                Sfo f;
                if (std::filesystem::exists(target))
                    f = metadata(s, dir);
                if((recreate&0xfffeffffu)>=2){
                    for(auto it=f.values.begin();it!=f.values.end();){
                        if(it->first[0]=='*' || it->first.rfind("ANYPS3_SECURE_",0)==0)it=f.values.erase(it);else ++it;
                    }
                }
                f.string("TITLE", text(param, 0, 128), 128);
                f.string("SUB_TITLE", text(param, 128, 128), 128);
                f.string("DETAIL", text(param, 256, 1024), 1024);
                f.string("SAVEDATA_DIRECTORY", dir, 32);
                f.string("SAVEDATA_LIST_PARAM", text(param, 1288, 8), 8);
                f.integer("ATTRIBUTE", be(param, 1280));
                f.integer("PARENTAL_LEVEL", be(param, 1284));
                f.string("CATEGORY","SD",4);
                f.string("PARAMS","",1024);f.string("PARAMS2","",12);
                f.values["ACCOUNT_ID"]={4,Bytes(16,'0'),16};
                for (unsigned i = 1296; i < 1552; ++i)
                    if (param[i])
                        throw std::runtime_error("SAVE nonzero metadata reserved bytes");
                const auto parent = root(s);
                std::filesystem::create_directories(parent);
                auto pattern = (parent / ".anyps3-save-XXXXXX").string();
                if (!::mkdtemp(pattern.data()))
                    throw std::system_error(errno,std::generic_category(),"SAVE staging allocation failed");
                d.staging = pattern;
                if (std::filesystem::exists(target) && (recreate&0xffff)<2)
                    for (const auto &e : std::filesystem::directory_iterator(target)) {
                        const auto n = e.path().filename().string();
                        const auto p = path(s, dir, n);
                        if (!std::filesystem::is_regular_file(p))
                            throw std::runtime_error("SAVE nonregular staging source");
                        write_file(d.staging / n, read(p, 64 * 1024 * 1024));
                    }
                write_file(d.staging / "PARAM.SFO", f.encode());
            } else if (std::filesystem::exists(target)) {
                (void)metadata(s, dir);
                const auto parent = root(s);
                auto pattern = (parent / ".anyps3-save-XXXXXX").string();
                if (!::mkdtemp(pattern.data()))
                    throw std::system_error(errno,std::generic_category(),"SAVE staging allocation failed");
                d.staging = pattern;
                for (const auto &p : files(s, dir))
                    write_file(d.staging / p.filename(), read(p, 64 * 1024 * 1024));
                write_file(d.staging / "PARAM.SFO", read(path(s, dir, "PARAM.SFO")));
            } else
                throw std::runtime_error("SAVE new save requires system metadata");
        } else if (q.attrs.count("elmSysFileParam"))
            throw std::runtime_error("SAVE load attempted metadata mutation");
        d.opened = true;
        if(q.attrs.count("elmIndicator"))savedata_ui_indicator(s,bytes);
        else result(d, "resOpenSaveData");
        return;
    }
    if (q.name == "readData" || q.name == "writeData" || q.name == "deleteData") {
        q.shape({"elmDataFileSet", "elmFileType", "elmFileName", "elmProgress"});
        if (!d.opened)
            throw std::runtime_error("SAVE file operation before open");
        const auto set = q.blob("elmDataFileSet", 48);
        const auto type = q.integer("elmFileType");
        const auto name = q.string("elmFileName");
        component(name, 12, true);
        if (type > 5 || (type<2 && filetype(name)!=1) || (type!=0 && filetype(name) != type) || name.rfind("PARAM", 0) == 0 ||
            (type == 1 &&
             (name.rfind("ICON", 0) == 0 || name.rfind("PIC", 0) == 0 || name.rfind("SND", 0) == 0)))
            throw std::runtime_error("SAVE invalid file type/name");
        const auto op = be(set, 0), offset = be(set, 32), size = be(set, 36);
        if (be(set, 4) || be(set, 8) != type || size > be(set, 40) || (size && !be(set, 44)) ||
            std::uint64_t(offset) + size > 64 * 1024 * 1024)
            throw std::runtime_error("SAVE invalid FileSet range/fields");
        const auto p = d.staging.empty() ? path(s, d.directory, name) : d.staging / name;
        d.secure_id.clear();
        if(type==0){
            d.secure_id.assign(set.begin()+12,set.begin()+28);
            const auto f=Sfo(read(d.staging.empty()?path(s,d.directory,"PARAM.SFO"):d.staging/"PARAM.SFO"));
            const auto it=f.values.find("ANYPS3_SECURE_"+name);
            if(std::filesystem::exists(p) && (it==f.values.end() || it->second.bytes!=d.secure_id))
                throw std::runtime_error("SAVE secure file ID mismatch or imported secure binding unsupported");
            static bool warned=false;if(!warned){std::fprintf(stderr,"SAVE: secure files use host plaintext with ID checks; console PFD/encryption unsupported\n");warned=true;}
        }
        if (q.name == "readData") {
            if (op)
                throw std::runtime_error("SAVE read opcode mismatch");
            if (!std::filesystem::exists(p)) {
                d.failed = true;
                result(d, "resReadData", 0x80029204);
                return;
            }
            auto data = read(p, 64 * 1024 * 1024);
            const auto n = offset >= data.size() ? 0 : std::min<std::size_t>(size, data.size() - offset);
            Bytes out;
            if (n)
                out.assign(data.begin() + offset, data.begin() + offset + n);
            enqueue(d, reply("resReadData", {{"elmResult", 0}, {"elmDataSize", static_cast<unsigned>(n)}}));
            // Firmware 0x2011eb24 tests FileGet.excSize (stack+360),
            // not the originally requested size: EOF has no raw packet.
            if (n)
                enqueue(d, std::move(out));
            return;
        }
        if ((d.mode & 1) || d.staging.empty())
            throw std::runtime_error("SAVE mutation during load");
        if (q.name == "deleteData") {
            if (op != 2)
                throw std::runtime_error("SAVE delete opcode mismatch");
            const bool removed = std::filesystem::remove(p);
            if(removed){auto f=Sfo(read(d.staging/"PARAM.SFO"));f.values.erase("*"+name);f.values.erase("ANYPS3_SECURE_"+name);write_file(d.staging/"PARAM.SFO",f.encode());}
            d.failed |= !removed;
            enqueue(d,
                    reply("resDeleteData", {{"elmResult", removed ? 0u : 0x80029204u}, {"elmDataSize", 0}}));
            return;
        }
        if (op != 1 && op != 3)
            throw std::runtime_error("SAVE write opcode mismatch");
        d.file = name;
        d.file_offset = offset;
        d.file_size = size;
        d.operation = op;
        d.writing = true;
        d.incoming.clear();
        // Firmware first reads an acceptance result, sends the raw bytes,
        // then reads a second result containing excSize (0x2011ec50..ec98).
        result(d, "resWriteData");
        if (!size)
            finish_write(d);
        return;
    }
    throw std::runtime_error("unsupported SAVE command: " + q.name);
}

static unsigned io_result(const std::system_error& e){
    return e.code().value()==ENOSPC || e.code().value()==EDQUOT?0x80029202u:0x80029201u;
}
static void request(PpuState& s,const Bytes& bytes){
    try{request_impl(s,bytes);}
    catch(const NoUser& e){
        Document q(bytes);auto& d=s.settings.savedata;d.failed=true;
        const auto root="res"+std::string(1,char(q.name[0]-'a'+'A'))+q.name.substr(1);
        result(d,root,0x8002926a);std::fprintf(stderr,"%s\n",e.what());
    }
    catch(const Corrupt& e){
        Document q(bytes);auto& d=s.settings.savedata;d.failed=true;
        const auto root="res"+std::string(1,char(q.name[0]-'a'+'A'))+q.name.substr(1);
        result(d,root,0x80029203);std::fprintf(stderr,"SAVE corrupt metadata: %s\n",e.what());
    }
    catch(const std::system_error& e){
        Document q(bytes);auto& d=s.settings.savedata;d.failed=true;
        const auto root="res"+std::string(1,char(q.name[0]-'a'+'A'))+q.name.substr(1);
        result(d,root,io_result(e));
        std::fprintf(stderr,"SAVE host I/O error: %s\n",e.what());
    }
}
} // namespace savedata_vsh
'''
