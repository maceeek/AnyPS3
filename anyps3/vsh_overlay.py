"""Host-only, thread-safe VSH panel composed at presentation, never in guest memory.

The same raster is used by SDL scanout and Vulkan WSI. UI updates publish an
immutable text snapshot; the RSX worker never reads mutable PpuState UI data.
"""
TYPES = r'''
#ifndef ANYPS3_VSH_OVERLAY_AVAILABLE
#define ANYPS3_VSH_OVERLAY_AVAILABLE 1
#include <mutex>
#include <string>
#include <vector>
#include <algorithm>
#include <dlfcn.h>
#if __has_include(<png.h>)
#include <png.h>
#endif
namespace anyps3_vsh_overlay {
struct Icon {unsigned width=0,height=0;std::vector<std::uint8_t> pixels;};
inline Icon decode_icon(const std::vector<std::uint8_t>& bytes){
 Icon result;if(bytes.empty())return result;
#if __has_include(<png.h>)
 auto* lib=dlopen("libpng16.so.16",RTLD_LAZY|RTLD_LOCAL);if(!lib)return result;
 auto begin=reinterpret_cast<decltype(&png_image_begin_read_from_memory)>(dlsym(lib,"png_image_begin_read_from_memory"));
 auto finish=reinterpret_cast<decltype(&png_image_finish_read)>(dlsym(lib,"png_image_finish_read"));
 auto free_image=reinterpret_cast<decltype(&png_image_free)>(dlsym(lib,"png_image_free"));
 if(begin && finish && free_image){png_image image{};image.version=PNG_IMAGE_VERSION;
  if(begin(&image,bytes.data(),bytes.size()) && image.width && image.height && image.width<=4096 && image.height<=4096){
   image.format=PNG_FORMAT_RGBA;result.pixels.resize(PNG_IMAGE_SIZE(image));
   if(finish(&image,nullptr,result.pixels.data(),0,nullptr)){result.width=image.width;result.height=image.height;}
   else result={};
  }
  free_image(&image);
 }
 dlclose(lib);
#endif
 return result;
}
struct Snapshot { Icon icon;bool visible=false;std::vector<std::string> lines;unsigned selected=~0u; };
inline std::mutex mutex;
inline Snapshot current;
inline void publish(Snapshot value){std::lock_guard<std::mutex> lock(mutex);current=std::move(value);}
struct Panel {unsigned x=0,y=0,width=0,height=0;std::vector<std::uint8_t> pixels;};
// Original 5x7 glyphs; lowercase is folded for this host UI. UTF-8 bytes
// outside ASCII are displayed as '?' (not interpreted as guest strings).
inline const char* glyph(char c){
 if(c>='a' && c<='z')c-=32;
 switch(c){
 case 'A':return "01110100011000111111100011000110001";
 case 'B':return "11110100011000111110100011000111110";
 case 'C':return "01111100001000010000100001000001111";
 case 'D':return "11110100011000110001100011000111110";
 case 'E':return "11111100001000011110100001000011111";
 case 'F':return "11111100001000011110100001000010000";
 case 'G':return "01111100001000010111100011000101111";
 case 'H':return "10001100011000111111100011000110001";
 case 'I':return "11111001000010000100001000010011111";
 case 'J':return "00111000100001000010100101001001100";
 case 'K':return "10001100101010011000101001001010001";
 case 'L':return "10000100001000010000100001000011111";
 case 'M':return "10001110111010110101100011000110001";
 case 'N':return "10001110011010110011100011000110001";
 case 'O':return "01110100011000110001100011000101110";
 case 'P':return "11110100011000111110100001000010000";
 case 'Q':return "01110100011000110001101011001001101";
 case 'R':return "11110100011000111110101001001010001";
 case 'S':return "01111100001000001110000010000111110";
 case 'T':return "11111001000010000100001000010000100";
 case 'U':return "10001100011000110001100011000101110";
 case 'V':return "10001100011000110001100010101000100";
 case 'W':return "10001100011000110101101011010101010";
 case 'X':return "10001100010101000100010101000110001";
 case 'Y':return "10001100010101000100001000010000100";
 case 'Z':return "11111000010001000100010001000011111";
 case '0':return "01110100011001110101110011000101110";
 case '1':return "00100011000010000100001000010001110";
 case '2':return "01110100010000100010001000100011111";
 case '3':return "11110000010000101110000010000111110";
 case '4':return "00010001100101010010111110001000010";
 case '5':return "11111100001000011110000010000111110";
 case '6':return "01110100001000011110100011000101110";
 case '7':return "11111000010001000100010000100001000";
 case '8':return "01110100011000101110100011000101110";
 case '9':return "01110100011000101111000010000101110";
 case ' ':return "00000000000000000000000000000000000";
 case '.':return "00000000000000000000000000011000110";
 case ':':return "00000011000110000000011000110000000";
 case '-':return "00000000000000011111000000000000000";
 case '/':return "00001000100001000100010000100010000";
 case '>':return "10000010000010000010001000100010000";
 case '%':return "11001110100001000100010001011110011";
 case '_':return "00000000000000000000000000000011111";
 case '?':default:return "01110100010000100010001000000000100";
 }
}
inline Panel raster(unsigned width,unsigned height,bool bgra=false){
 Snapshot snap;{std::lock_guard<std::mutex> lock(mutex);snap=current;}
 Panel p;if(!snap.visible || width<64 || height<48)return p;
 p.width=std::min(width-32,900u);p.height=std::min(height-32,unsigned(40+snap.lines.size()*22));
 if(!snap.icon.pixels.empty())p.height=std::min(height-32,p.height+100);
 p.x=(width-p.width)/2;p.y=(height-p.height)/2;p.pixels.resize(std::size_t(p.width)*p.height*4);
 auto pixel=[&](unsigned x,unsigned y,unsigned r,unsigned g,unsigned b){if(x>=p.width || y>=p.height)return;
  auto at=(std::size_t(y)*p.width+x)*4;p.pixels[at]=bgra?b:r;p.pixels[at+1]=g;p.pixels[at+2]=bgra?r:b;p.pixels[at+3]=255;};
 for(unsigned y=0;y<p.height;++y)for(unsigned x=0;x<p.width;++x){
  const bool border=x<2 || y<2 || x+2>=p.width || y+2>=p.height;
  pixel(x,y,border?96:12,border?160:20,border?240:36);
 }
 if(!snap.icon.pixels.empty()){
  const auto iw=std::min(160u,snap.icon.width),ih=std::min(90u,snap.icon.height);
  for(unsigned y=0;y<ih;++y)for(unsigned x=0;x<iw;++x){
   auto at=(std::size_t(y*snap.icon.height/ih)*snap.icon.width+x*snap.icon.width/iw)*4;
   pixel(14+x,14+y,snap.icon.pixels[at],snap.icon.pixels[at+1],snap.icon.pixels[at+2]);
  }
 }
 for(unsigned row=0;row<snap.lines.size();++row){
  unsigned x=14,y=18+row*22+(snap.icon.pixels.empty()?0:100);const bool selected=row==snap.selected;
  if(selected)for(unsigned by=y-3;by<std::min(y+18,p.height-2);++by)for(unsigned bx=4;bx+4<p.width;++bx)pixel(bx,by,30,72,120);
  for(unsigned char c:snap.lines[row]){
   if(x+12>=p.width)break;
   const auto* bits=glyph(c<128?char(c):'?');
   for(unsigned gy=0;gy<7;++gy)for(unsigned gx=0;gx<5;++gx)if(bits[gy*5+gx]=='1')
    for(unsigned sy=0;sy<2;++sy)for(unsigned sx=0;sx<2;++sx)pixel(x+gx*2+sx,y+gy*2+sy,240,244,255);
   x+=12;
  }
 }
 return p;
}
inline void composite(std::vector<std::uint8_t>& rgba,unsigned width,unsigned height){
 auto p=raster(width,height);if(rgba.size()!=std::size_t(width)*height*4)return;
 for(unsigned y=0;y<p.height;++y)std::copy_n(p.pixels.data()+std::size_t(y)*p.width*4,p.width*4,
  rgba.data()+(std::size_t(p.y+y)*width+p.x)*4);
}
}
#endif
'''
