import sys
import os
import glfw
from OpenGL.GL import *
import OpenGL.GL.shaders
import numpy as np
import pyrr
import math
from PIL import Image, ImageDraw, ImageFont

from read_geometry import read_geometry, read_morph_data


def normalize_mesh(v, scale=1.0):
    # center bounding box at origin
    # scale longest side to scale (default 1)
    bbmax=np.max(v,axis=0)
    bbmin=np.min(v,axis=0)
    v = v - 0.5*(bbmax+bbmin)
    v = scale * v / np.max(bbmax-bbmin)
    return v

# adopted from lapy
def vertex_normals(v,t):
    """
    get_vertex_normals(v,t) computes vertex normals
        Triangle normals around each vertex are averaged, weighted
        by the angle that they contribute.
        Ordering is important: counterclockwise when looking
        at the triangle from above.
    :return:  n - normals (num vertices X 3 )
    """
    import sys
    # Compute vertex coordinates and a difference vector for each triangle:
    v0 = v[t[:, 0], :]
    v1 = v[t[:, 1], :]
    v2 = v[t[:, 2], :]
    v1mv0 = v1 - v0
    v2mv1 = v2 - v1
    v0mv2 = v0 - v2
    # Compute cross product at every vertex
    # will all point in the same direction but have different lengths depending on spanned area
    cr0 = np.cross(v1mv0, -v0mv2)
    cr1 = np.cross(v2mv1, -v1mv0)
    cr2 = np.cross(v0mv2, -v2mv1)
    # Add normals at each vertex (there can be duplicate indices in t at vertex i)
    n = np.zeros(v.shape)
    np.add.at(n, t[:, 0], cr0)
    np.add.at(n, t[:, 1], cr1)
    np.add.at(n, t[:, 2], cr2)
    # Normalize normals
    ln = np.sqrt(np.sum(n * n, axis=1))
    ln[ln < sys.float_info.epsilon] = 1  # avoid division by zero
    n = n / ln.reshape(-1, 1)
    return n


def heat_color(values,invert=False):
    # values (1 dim array length n) will receive gradient between -1 and 1
    # nan will return (nan,nan,nan)
    # returns colors (r,g,b)  as n x 3 array
    if invert:
        values=-1.0*values
    vabs = np.abs(values)
    colors = np.zeros((vabs.size,3),dtype=np.float32)
    crb = 0.5625 + 3 * 0.4375 * vabs
    cg = 1.5 * (vabs - (1.0/3.0))
    n1 = values < -1.0
    nm = (values >= -1.0) & (values < -(1.0/3.0))
    n0 = (values >= -(1.0/3.0)) & (values < 0)
    p0 = (values >= 0) & (values < (1.0/3.0))
    pm = (values >= (1.0/3.0)) & (values < 1.0)
    p1 = values >= 1.0
    # fill in colors for the 5 blocks
    colors[n1,1:3] = 1.0       # bright blue
    colors[nm,1] = cg[nm]      # cg incresing green channel
    colors[nm,2] = 1.0         # and keeping blue on full
    colors[n0,2] = crb[n0]     # crb incresing blue channel
    colors[p0,0] = crb[p0]     # crb incresing red channel
    colors[pm,1] = cg[pm]      # cg incresing green channel
    colors[pm,0] = 1.0         # and keeping red on full
    colors[p1,0:2] = 1.0       # yellow
    colors[np.isnan(values),:] = np.nan
    return colors

def rescale_overlay(values, minval=None, maxval=None):
    # rescales values for color computation
    # minval and maxval are two positive floats (maxval>minval)
    # values between -minval and minval will be masked (np.nan)
    # others will be shifted towards zero (from both sides)
    # and scaled so that -maxval and maxval are at -1 and +1
    valsign = np.sign(values)
    valabs = np.abs(values)
    realmin = np.min(values)
    if maxval is None:
        maxval=np.max(valabs)
    if minval is None:
        minval = max(0.0, np.min(valabs))
    if maxval < 0 or minval < 0:
        print("resacle_overlay ERROR: min and maxval should both be positive!")
        exit(1)
    print("Using min {:.2f} and max {:.2f}".format(minval,maxval))
    # rescale map symetrically to -1 .. 1 (keeping minval at 0)
    # mask values below minval 
    values[valabs<minval] = np.nan
    # shift towards 0 from both sides
    values = values - valsign * minval
    # rescale so that former maxval is at 1 (and -1 for negative values)
    values = values / (maxval - minval)
    return values, minval, maxval, (realmin<0 and realmin < - minval)

def binary_color(values, thres, color_low, color_high):
    # creates a binary colormap where values below thres are color_low, the others color_high
    # values is a 1-dim array
    # thres a float
    # color_low and color_high can be float (gray scale), or 1x3 array of RGB
    if np.isscalar(color_low):
        color_low = np.array((color_low,color_low,color_low),dtype=np.float32)
    if np.isscalar(color_high):
        color_high = np.array((color_high,color_high,color_high),dtype=np.float32)
    colors=np.empty((values.size,3),dtype=np.float32)
    colors[values<thres,:] = color_low
    colors[values>=thres,:] = color_high
    return colors 

def mask_label(values,labelpath=None):
    # apply a labelfile as mask
    # values is a 1-dim array
    # labelfile freesurfer format has indices of values that should be kept
    # all other values will be set to np.nan
    if not labelpath:
        return values
    # this is the mask of vertices to keep, e.g. cortex labels
    maskvids = np.loadtxt(labelpath, dtype=int, skiprows=2, usecols=[0])
    imask = np.ones(values.shape,dtype=bool)
    imask[maskvids] = False
    values[imask] = np.nan    
    return values

def prepare_gerometry(surfpath, overlaypath=None, curvpath=None, labelpath=None, minval=None, maxval=None, invert=False):
    # prepare meshdata and tringles
    # surfpath : file path of surface file, usually lh or rh.pial_semi_inflated
    # overlaypath : file path of ovlerlay file
    # curvpath : file path of curvature file usually lh or rh.curv
    # labelpath : file path of label file (mask), usually cortex.label
    # minval : min threshold to stop coloring (-minval used for neg values)
    # maxval : max value to saturate (-maxval used for negative values)
    # invert : invert color map


    # read vertices and triangels
    surf = read_geometry(surfpath, read_metadata=False)
    vertices = normalize_mesh(np.array(surf[0], dtype=np.float32),1.85)
    triangles = np.array(surf[1], dtype=np.uint32)
    # compute vertex normals
    vnormals = np.array(vertex_normals(vertices,triangles), dtype=np.float32)
    # read curvature
    if curvpath:
        curv = read_morph_data(curvpath)
        sulcmap = binary_color(curv,0.0,color_low=0.5,color_high=0.33)
    else:
        # if no curv pattern, color mesh in mid-gray
        sulcmap = 0.5 * np.ones(vertices.shape,dtype=np.float32)
    # read map (stats etc)
    if overlaypath:
        mapdata = read_morph_data(overlaypath)
        mapdata, fmin, fmax, neg = rescale_overlay(mapdata, minval, maxval)
        # mask map with label
        mapdata = mask_label(mapdata,labelpath)
        # compute color
        colors = heat_color(mapdata, invert)
        missing = np.isnan(mapdata)
        colors[missing,:] = sulcmap[missing,:]
    else:
        colors=sulcmap
    # concatenate matrices
    vertexdata = np.concatenate((vertices,vnormals,colors),axis=1)
    return vertexdata, triangles, fmin, fmax, neg



#def key_event(window,key,scancode,action,mods):
#    """ Handle keyboard events
#    """
#    if action == glfw.PRESS and key == glfw.KEY_RIGHT:
#        print("right")


def init_window(width,height,title="PyOpenGL",visible=True):
    # create window with width, height, title
    # if visible False, hide window 
    if not glfw.init():
        return False
 
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    if not visible:
        glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
    window = glfw.create_window(width, height, title, None, None)
    if not window:
        glfw.terminate()
        return false
    # Enable key events
    glfw.set_input_mode(window,glfw.STICKY_KEYS,GL_TRUE) 
    # Enable key event callback
    #glfw.set_key_callback(window,key_event)
    glfw.make_context_current(window)
    # vsync and glfw do not play nice.  when vsync is enabled mouse movement is jittery.
    glfw.swap_interval(0)
    return window


def setup_shader(meshdata, triangles, width, height):
    # meshdata is array of shape (n, 9) and dtype np.float32 where
    #   the first 3 columns are the vertex coordinates
    #   the next  3 columns are the vertex normals
    #   the final 3 columns are the color RGB values
    # triangles is array of shape (m, 3) with triangle indices
    # width and height of the window to set perspective projection

    VERTEX_SHADER = """
 
        #version 330

        layout (location = 0) in vec3 aPos;
        layout (location = 1) in vec3 aNormal;
        layout (location = 2) in vec3 aColor;
        
        out vec3 FragPos;
        out vec3 Normal;
        out vec3 Color;
        
        uniform mat4 transform; 
        uniform mat4 model;
        uniform mat4 view;
        uniform mat4 projection;
 
        void main()
        {
          gl_Position = projection * view * model * transform * vec4(aPos, 1.0f);
          FragPos = vec3(model * transform * vec4(aPos, 1.0));
          Normal = mat3(transpose(inverse(view * model * transform))) * aNormal; // normal matrix should be computed outside and passed!
          Color = aColor;
        }
 
    """
 
    FRAGMENT_SHADER = """
        #version 330
 
        in vec3 Normal;
        in vec3 FragPos;
        in vec3 Color; 

        out vec4 FragColor;
 
        uniform vec3 lightColor;

        void main()
        {
          // ambient
          float ambientStrength = 0.0;
          vec3 ambient = ambientStrength * lightColor;

          // diffuse
          vec3 norm = normalize(Normal);
          // key light (overhead)
          vec3 lightPos1 = vec3(0.0,5.0,5.0);
          vec3 lightDir = normalize(lightPos1 - FragPos);
          float diff = max(dot(norm, lightDir), 0.0);
          float key = 0.6;
          vec3 diffuse = key * diff * lightColor;

          // headlight (at camera)
          vec3 lightPos2 = vec3(0.0,0.0,5.0);
          lightDir = normalize(lightPos2 - FragPos);
          vec3 ohlightDir = lightDir;
          diff = max(dot(norm, lightDir), 0.0);
          diffuse = diffuse + 0.68  * key * diff * lightColor;

          // fill light (from below)
          vec3 lightPos3 = vec3(0.0,-5.0,5.0);
          lightDir = normalize(lightPos3 - FragPos);
          diff = max(dot(norm, lightDir), 0.0);
          diffuse = diffuse + 0.6  * key * diff * lightColor;

          // left right back lights
          vec3 lightPos4 = vec3(5.0,0.0,-5.0);
          lightDir = normalize(lightPos4 - FragPos);
          diff = max(dot(norm, lightDir), 0.0);
          diffuse = diffuse + 0.52 * key * diff * lightColor;
          vec3 lightPos5 = vec3(-5.0,0.0,-5.0);
          lightDir = normalize(lightPos5 - FragPos);
          diff = max(dot(norm, lightDir), 0.0);
          diffuse = diffuse + 0.52 * key * diff * lightColor;


          // specular 
          float specularStrength = 0.5;
          vec3 viewDir = normalize(-FragPos); // the viewer is always at (0,0,0) in view-space, so viewDir is (0,0,0) - Position => -Position
          vec3 reflectDir = reflect(ohlightDir, norm);  
          float spec = pow(max(dot(viewDir, reflectDir), 0.0), 32);
          vec3 specular = specularStrength * spec * lightColor; 

          // final color
          vec3 result = (ambient + diffuse + specular) * Color;
          //vec3 result = (ambient + diffuse) * Color;
          FragColor = vec4(result, 1.0);
        }
 
    """
  
 
    # Create Vertex Buffer object in gpu
    VBO = glGenBuffers(1)
    # Bind the buffer
    glBindBuffer(GL_ARRAY_BUFFER, VBO)
    glBufferData(GL_ARRAY_BUFFER, meshdata.nbytes , meshdata, GL_STATIC_DRAW)

    # Create Vertex Array object
    VAO = glGenVertexArrays(1)
    # Bind array
    glBindVertexArray(VAO)
    glBufferData(GL_ARRAY_BUFFER, meshdata.nbytes , meshdata, GL_STATIC_DRAW)

    #Create Element Buffer Object
    EBO = glGenBuffers(1)
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, EBO)
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, triangles.nbytes, triangles, GL_STATIC_DRAW)
 
    # Compile The Program and shaders
    shader = OpenGL.GL.shaders.compileProgram(OpenGL.GL.shaders.compileShader(VERTEX_SHADER, GL_VERTEX_SHADER),
                                              OpenGL.GL.shaders.compileShader(FRAGMENT_SHADER, GL_FRAGMENT_SHADER))

    # get the position from shader
    position = glGetAttribLocation(shader, 'aPos')
    glVertexAttribPointer(position, 3, GL_FLOAT, GL_FALSE, 9*4, ctypes.c_void_p(0))
    glEnableVertexAttribArray(position)

    vnormalpos = glGetAttribLocation(shader, 'aNormal')
    glVertexAttribPointer(vnormalpos, 3, GL_FLOAT, GL_FALSE, 9*4, ctypes.c_void_p(3*4))
    glEnableVertexAttribArray(vnormalpos)

    colorpos = glGetAttribLocation(shader, 'aColor')
    glVertexAttribPointer(colorpos, 3, GL_FLOAT, GL_FALSE, 9*4, ctypes.c_void_p(6*4))
    glEnableVertexAttribArray(colorpos)
 
    glUseProgram(shader)
 
    glClearColor(0.0, 0.0, 0.0, 1.0)
    glEnable(GL_DEPTH_TEST)

    #Creating Projection Matrix
    view = pyrr.matrix44.create_from_translation(pyrr.Vector3([0.0,0.0,-5.0] ))
    projection = pyrr.matrix44.create_perspective_projection(20.0, width/height, 0.1, 100.0)
    model = pyrr.matrix44.create_from_translation(pyrr.Vector3([0.0,0.0,0.0]))
 
    view_loc = glGetUniformLocation(shader, "view")
    proj_loc = glGetUniformLocation(shader, "projection")
    model_loc = glGetUniformLocation(shader, "model")
 
    glUniformMatrix4fv(view_loc, 1, GL_FALSE, view)
    glUniformMatrix4fv(proj_loc, 1, GL_FALSE, projection)
    glUniformMatrix4fv(model_loc, 1, GL_FALSE, model)

    # setup light color
    lightColor_loc = glGetUniformLocation(shader, "lightColor")
    glUniform3f(lightColor_loc, 1.0,1.0,1.0)

    return shader

def capture_window(width,height):
    # capture GL region (0,0) .. (width,height) into PIL Image
    if sys.platform == "darwin":
        # not sure why on mac the drawing area is 4 times as large (2x2):
        width = 2 * width
        height = 2 * height
    glPixelStorei(GL_PACK_ALIGNMENT, 1) # may not be needed
    img_buf = glReadPixels(0, 0, width, height, GL_RGB, GL_UNSIGNED_BYTE)
    image = Image.frombytes("RGB", (width, height), img_buf)
    image = image.transpose(Image.FLIP_TOP_BOTTOM)
    if sys.platform == "darwin":
        image.thumbnail((0.5*width,0.5*height), Image.Resampling.LANCZOS)
    return image

def create_colorbar(fmin,fmax,invert,neg=True):
    # fmin: abs of min value that receives color (threshold)
    # fmax: abs of max value where color saturates
    # invert : color invert
    # neg : also show negative axis
    cwidth=200
    cheight=30
    img = Image.new('RGB', (cwidth, cheight), color = (90,90,90))
    values = np.nan * np.ones((cwidth))
    gapspace = 0
    if fmin > 0.01: 
        # leave gray gap
        num=int(0.42*cwidth)
        gapspace = 0.08*cwidth
    else:
        num=int(0.5*cwidth)
    if not neg:
        num = num * 2
        gapspace = gapspace * 2
    vals = np.linspace(0.01,1,num)
    if not neg:
        values[-vals.size:] = vals
    else:
        values[:vals.size] = -1.0*np.flip(vals)
        values[-vals.size:] = vals

    colors = heat_color(values,invert)
    colors[np.isnan(values),:] = 0.33*np.ones((1,3))
    img_bar = np.uint8(np.tile(colors,(cheight,1,1))*255)
    # pad with black
    img_buf = np.zeros((cheight+20,cwidth+20,3),dtype=np.uint8)
    img_buf[3:cheight+3,10:cwidth+10,:] = img_bar
    image = Image.fromarray(img_buf)

    font = ImageFont.truetype("roboto-regular.ttf", 12)
    if neg:
        # Left
        caption=" <{:.2f}".format(-fmax)
        xpos = 0 #10- 0.5*(font.getlength(caption))
        ImageDraw.Draw(image).text((xpos, image.height-17), caption, (220,220,220), font=font)
        # Right
        caption=">{:.2f} ".format(fmax)
        xpos = image.width - (font.getlength(caption))
        ImageDraw.Draw(image).text((xpos, image.height-17), caption, (220,220,220), font=font)
        if gapspace == 0:
            caption="0"
            xpos = 0.5 * image.width - 0.5 * font.getlength(caption)
            ImageDraw.Draw(image).text((xpos, image.height-17), caption, (220,220,220), font=font)
        else:
            caption="{:.2f}".format(-fmin)
            xpos = 0.5 * image.width - 0.5 * font.getlength(caption) - gapspace - 5
            ImageDraw.Draw(image).text((xpos, image.height-17), caption, (220,220,220), font=font)
            caption="{:.2f}".format(fmin)
            xpos = 0.5 * image.width - 0.5 * font.getlength(caption) + gapspace + 5
            ImageDraw.Draw(image).text((xpos, image.height-17), caption, (220,220,220), font=font)
    else:
        # Right
        caption=">{:.2f} ".format(fmax)
        xpos = image.width - (font.getlength(caption))
        ImageDraw.Draw(image).text((xpos, image.height-17), caption, (220,220,220), font=font)
        # Left
        caption=" {:.2f}".format(fmin)
        xpos = gapspace
        if gapspace == 0:
            caption=" 0"
            xpos = 5
        ImageDraw.Draw(image).text((xpos, image.height-17), caption, (220,220,220), font=font)

    return image


def snap4(lhoverlaypath, rhoverlaypath, fthresh=None, fmax=None, sid="fsaverage", sdir=None,
           caption=None, invert=False, labelname="cortex.label", surfname="pial_semi_inflated",
           curvname="curv", colorbar=True, outpath=None):
    # Function to snap 4 views, front and back for left and right
    #
    # lh rhoverlaypath : path to the overlay files for left and right hemi (FreeSurfer format)
    # fthresh : pos float value under which (absolute value) no color is shown
    # fmax    : pos float value above which (absolute value) color is saturated 
    # sid     : subject id, default fsaverage
    # sdir    : subject dir (use $FREESURFER_HOME/subjects/ as default in future)
    # caption : caption text on image
    # invert  : color invert (blue positive, red negative)
    # labelname : label for masking, usually cortex.label
    # surfname : surface to display values on , usually pial_semi_inflated from fsaverage
    # curvname : curvature file for texture in non-colored regions, default curv
    # colorbar : show colorbar in image
    # outpath : path and filename of output image

    # setup window (keep this aspect ratio, as the mesh scale and distances are set accordingly)
    wwidth=540
    wheight=450
    visible=False
    window = init_window(wwidth,wheight,"WhipperSnapper 2.0",visible)
    if not window:
        return False # need raise error here in future

    # set up matrices to show object left and right side:
    rot_z = pyrr.Matrix44.from_z_rotation(-0.5 * math.pi)
    rot_x = pyrr.Matrix44.from_x_rotation(0.5 * math.pi)
    #rot_y = pyrr.Matrix44.from_y_rotation(math.pi/6)
    viewLeft = rot_x * rot_z
    rot_y = pyrr.Matrix44.from_y_rotation(math.pi)
    viewRight = rot_y * viewLeft
    transl = pyrr.Matrix44.from_translation((0,0,0.4))

    for hemi in ("lh","rh"):
        meshpath = os.path.join(sdir,sid,"surf",hemi+"."+surfname)
        curvpath = None
        if curvname:
            curvpath = os.path.join(sdir,sid,"surf",hemi+"."+curvname)
        labelpath = None
        if labelname:
            labelpath = os.path.join(sdir,sid,"label",hemi+"."+labelname)
        if hemi=="lh":
            overlaypath=lhoverlaypath
        else:
            overlaypath=rhoverlaypath

        # load and colorzie data
        meshdata, triangles, fthresh, fmax, neg = prepare_gerometry(meshpath, overlaypath, curvpath, labelpath, fthresh, fmax, invert)
        # upload to GPU and compile shaders
        shader = setup_shader(meshdata, triangles, wwidth, wheight)

        # draw
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        transformLoc = glGetUniformLocation(shader, "transform")
        viewmat = viewLeft
        if hemi=="lh":
            viewmat=transl*viewmat
        glUniformMatrix4fv(transformLoc, 1, GL_FALSE, viewmat )
        glDrawElements(GL_TRIANGLES,triangles.size, GL_UNSIGNED_INT,  None)
        
        im1 = capture_window(wwidth,wheight)

        glfw.swap_buffers(window)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        viewmat = viewRight
        if hemi=="rh":
            viewmat=transl*viewmat
        glUniformMatrix4fv(transformLoc, 1, GL_FALSE, viewmat )
        glDrawElements(GL_TRIANGLES,triangles.size, GL_UNSIGNED_INT,  None)

        im2 = capture_window(wwidth,wheight)

        if hemi=="lh":
            lhimg = Image.new('RGB', (im1.width, im1.height + im2.height))
            lhimg.paste(im1, (0, 0))
            lhimg.paste(im2, (0, im1.height))
        else:
            rhimg = Image.new('RGB', (im1.width, im1.height + im2.height))
            rhimg.paste(im2, (0, 0))
            rhimg.paste(im1, (0, im2.height))

    image = Image.new('RGB', (lhimg.width + rhimg.width, lhimg.height))
    image.paste(lhimg, (0,0))
    image.paste(rhimg, (im1.width, 0))

    if caption:
        font = ImageFont.truetype("roboto-regular.ttf", 20)
        xpos = 0.5*(image.width-font.getlength(caption))
        ImageDraw.Draw(image).text((xpos, image.height-40), caption, (220,220,220), font=font)

    if colorbar:
        bar = create_colorbar(fthresh,fmax,invert,neg)
        xpos = int(0.5*(image.width-bar.width))
        ypos = int(0.5*(image.height-bar.height))
        image.paste(bar,(xpos,ypos))

    if outpath:
        image.save(outpath)



def show_window(hemi,overlaypath, fthresh=None, fmax=None, sid="fsaverage", sdir=None,
           caption=None, invert=False, labelname="cortex.label", surfname="pial_semi_inflated",
           curvname="curv"):
    # function to show an interactive window
    #
    # hemi : what hemi load
    # overlaypath : path to the overlay files for the specified hemi (FreeSurfer format)
    # fthresh : pos float value under which (absolute value) no color is shown
    # fmax    : pos float value above which (absolute value) color is saturated 
    # sid     : subject id, default fsaverage
    # sdir    : subject dir (use $FREESURFER_HOME/subjects/ as default in future)
    # caption : caption text on image
    # invert  : color invert (blue positive, red negative)
    # labelname : label for masking, usually cortex.label
    # surfname : surface to display values on , usually pial_semi_inflated from fsaverage
    # curvname : curvature file for texture in non-colored regions, default curv

    wwidth=720
    wheight=600
    window = init_window(wwidth,wheight,"WhipperSnapper 2.0",visible=True)
    if not window:
        return False

    meshpath = os.path.join(sdir,sid,"surf",hemi+"."+surfname)
    curvpath = None
    if curvname:
        curvpath = os.path.join(sdir,sid,"surf",hemi+"."+curvname)
    labelpath = None
    if labelname:
        labelpath = os.path.join(sdir,sid,"label",hemi+"."+labelname)


    meshdata, triangles = prepare_gerometry(meshpath, overlaypath, curvpath, labelpath, fthresh, fmax)

    shader = setup_shader(meshdata, triangles, wwidth, wheight)

    # set up matrices to show object left and right side:
    rot_z = pyrr.Matrix44.from_z_rotation(-0.5 * math.pi)
    rot_x = pyrr.Matrix44.from_x_rotation(0.5 * math.pi)
    viewLeft = rot_x * rot_z
    rot_y = pyrr.Matrix44.from_y_rotation(math.pi)
    viewRight = rot_y * viewLeft
    rot_y = pyrr.Matrix44.from_y_rotation(0) 

    print()
    print("Keys:")
    print("Left - Right : Rotate Geometry")
    print("ESC          : Quit")
    print()


    ypos = 0
    while glfw.get_key(window,glfw.KEY_ESCAPE) != glfw.PRESS and not glfw.window_should_close(window):
        glfw.poll_events()
 
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
 
        transformLoc = glGetUniformLocation(shader, "transform")
        glUniformMatrix4fv(transformLoc, 1, GL_FALSE, rot_y * viewLeft )
        #rot_y = pyrr.Matrix44.from_y_rotation(0.8 * glfw.get_time())

        if glfw.get_key(window,glfw.KEY_RIGHT) == glfw.PRESS:
            ypos = ypos + 0.0004
        if glfw.get_key(window,glfw.KEY_LEFT) == glfw.PRESS:
            ypos = ypos - 0.0004
        rot_y = pyrr.Matrix44.from_y_rotation(ypos)

        # Draw 
        glDrawElements(GL_TRIANGLES,triangles.size, GL_UNSIGNED_INT,  None)
 
        glfw.swap_buffers(window)
 
    glfw.terminate()



if __name__ == "__main__":


    inspect_hemi=None

    if inspect_hemi:
        show_window(inspect_hemi,"lh.thickness", sdir="/Applications/freesurfer/7.3.2/subjects/")
    else:
        snap4("../../lh.thickness","../../rh.thickness",
              sdir="/Applications/freesurfer/7.3.2/subjects/",
              caption="Super cool WhipperSnapper 2.0",fthresh=2,fmax=4.0,invert=False,
              colorbar=True,
              outpath="snap4.png")



# headless docker test using xvfb:
# Note, xvfb is a display server implemening the X11 protocol, performing all graphics on memory
# glfw needs a windows to render even if that is invisible, so above code
# will not work via ssh or on a headless server. xvfb can solve this by wrapping:
#docker run --name headless_test -ti -v$(pwd):/test ubuntu /bin/bash
#apt update && apt install -y python3 python3-pip xvfb
#pip3 install pyopengl glfw pillow numpy pyrr
#xvfb-run python3 test4.py

# instead of the above one could really do headless off screen rendering via EGL (preferred)
# or OSMesa. The latter looks doable. EGL looks tricky. 
# EGL is part of any modern NVIDIA driver
# OSMesa needs to be installed, but should work almost everywhere

# using EGL maybe like this:
# https://github.com/eduble/gl
# or via these bindings:
# https://github.com/perey/pegl

# or OSMesa
# https://github.com/AntonOvsyannikov/DockerGL


