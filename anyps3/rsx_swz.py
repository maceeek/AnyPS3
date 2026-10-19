"""RSX packed Morton storage, derived from RPCS3 ffba2bf0.

Utils/image_utils.hpp:73-145 and Common/TextureUtils.cpp:258-271:
the largest swizzle element is 32 bits; wide float pixels use adjacent
elements in the expanded x dimension, not one 64/128-bit Morton element.
"""

CPP = r'''
static std::uint64_t swizzle_byte(unsigned width,unsigned height,unsigned x,unsigned y,unsigned bpp,unsigned byte=0){
    const unsigned block=std::min(bpp,4u),words=bpp/block;
    width*=words;x=x*words+byte/block;
    std::uint64_t index=0;unsigned bit=0;
    for(unsigned i=0;(width>>i)>1 || (height>>i)>1;++i){
        if((width>>i)>1)index|=std::uint64_t((x>>i)&1)<<bit++;
        if((height>>i)>1)index|=std::uint64_t((y>>i)&1)<<bit++;
    }
    return index*block+byte%block;
}
'''
