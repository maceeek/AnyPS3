"""SPU relocation content index (spu_reloc.py SpuRelocIndex::find) with a binary search over full 16-byte windows.

Independent expectation: a brute-force scan written in the test over every 4-byte-aligned window of every source, selecting the first
match in (size desc, image, pc) order (size = min(16, bytes left) rounded to words, eligible only if it fits below the LS end), for LS
contents built from source fragments (many shared first words, duplicated sources across images, near-LS-end pcs, mutations).
"""
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.program import compile_program
from tests.test_anyps3 import BLR, program
from tests.test_spu_block import image

MAIN = r'''
#include <cassert>
#include <random>
struct Src {unsigned image,pc;std::vector<char> bytes;};
int main(){
    std::mt19937 rng(7);
    std::vector<Src> sources;
    const std::uint32_t common[]={0x24004080u,0x24ff0081u,0x1c00c183u,0x40800003u};   // shared first words
    for(unsigned n=0;n<60;++n){
        Src x{n%3,0x1000u*(n+1)+((n*52)&0xffc),{}};
        const unsigned words=1+rng()%40;
        for(unsigned w=0;w<words;++w){const std::uint32_t v=rng()%3?common[rng()%4]:std::uint32_t(rng()%5);
            for(unsigned b=0;b<4;++b)x.bytes.push_back(char(v>>(24-8*b)));}
        sources.push_back(x);
        if(n%7==0){Src y=x;y.image=(x.image+1)%3;y.pc+=0x40000;sources.push_back(y);}  // same bytes in another image
    }
    std::vector<SpuRelocSource> list;for(const auto& x:sources)list.push_back({x.image,x.pc,unsigned(x.bytes.size()),x.bytes.data()});
    std::vector<std::uint8_t> ls(0x40000);
    const SpuRelocIndex index(list.data(),list.data()+list.size());
    unsigned hits=0,tail_hits=0;
    for(unsigned round=0;round<20000;++round){
        const auto& x=sources[rng()%sources.size()];
        unsigned pc=(rng()%8==0)?0x40000-4*(1+rng()%6):4*(rng()%0x10000);
        std::fill(ls.begin(),ls.end(),std::uint8_t(0));
        const unsigned off=4*(rng()%(x.bytes.size()/4));
        for(unsigned i=0;i<20 && pc+i<0x40000;++i)ls[pc+i]=off+i<x.bytes.size()?std::uint8_t(x.bytes[off+i]):std::uint8_t(rng()%3);
        if(rng()%5==0)ls[pc+4*(rng()%4)%std::max(4u,std::min(16u,0x40000-pc))]^=1;
        // brute force
        bool found=false;unsigned best_size=0,best_image=0,best_pc=0;
        for(const auto& s:sources)for(unsigned o=0;o+4<=s.bytes.size();o+=4){
            const unsigned size=std::min<unsigned>(16,unsigned(s.bytes.size())-o)&~3u;
            if(size>0x40000-pc || std::memcmp(ls.data()+pc,s.bytes.data()+o,size))continue;
            const unsigned cp=s.pc+o;
            if(!found || size>best_size || (size==best_size && (s.image<best_image || (s.image==best_image && cp<best_pc)))){found=true;best_size=size;best_image=s.image;best_pc=cp;}
        }
        std::uint32_t original=0,img=0;const bool got=index.find(ls.data(),pc,original,img);
        if(got!=found || (found && (original!=best_pc || img!=best_image))){
            std::fprintf(stderr,"round %u pc=%x: index %d %x/%u vs reference %d %x/%u\n",round,pc,got,original,img,found,best_pc,best_image);std::abort();}
        hits+=found;tail_hits+=found && best_size<16;
    }
    std::printf("hits=%u tail_hits=%u\n",hits,tail_hits);assert(hits>5000 && tail_hits>100);
    std::puts("ok");
}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class RelocIndex(unittest.TestCase):
    def test_find_equals_brute_force(self):
        img = image({0xffc: 0x33000105, 0x1000: 1, 0x1004: 0x1c00c183, 0x1008: 0x35000280})
        translated = replace(compile_program(ElfImage.parse(program(BLR)), process_entry=True), spu_images=(img,))
        source = translated.cpp().replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        self.assertIn('class SpuRelocIndex', source)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'test.cpp').write_text(source + MAIN)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-pthread', str(root/'test.cpp'),
                                    '-o', str(root/'test')], capture_output=True, text=True, timeout=900)
            self.assertEqual(build.returncode, 0, build.stderr[-6000:])
            run = subprocess.run([str(root/'test')], capture_output=True, text=True, timeout=300)
            self.assertEqual(run.returncode, 0, run.stderr[-4000:] + run.stdout[-2000:])
            self.assertIn('ok', run.stdout)


if __name__ == '__main__':
    unittest.main()
