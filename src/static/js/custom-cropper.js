/*
 * profile pic editor plugin
 *
 */

// Assuming modal and cropper are still valid and compatible.
// If not, they need to be updated/replaced as well.
var modal = require('./modal');
var cropper = require('./cropper');

var options = {
    inputPrefix: 'photo',
    aspectRatio: 2/1
};


var setup = function(file, args) {
    if (args.inputPrefix) options.inputPrefix = args.inputPrefix;
    if (args.aspectRatio) options.aspectRatio = args.aspectRatio;
    if (args.callback) options.callback = args.callback;
    drawImage(file);
}
 
/*
 * Read uploaded image into base64
 * 
 */
var drawImage = function(file, prefix) {
    var fileReader = new FileReader();

    //do we have an input prefix
    if (prefix) options.inputPrefix = prefix;

    //check if file is not too big
    if (file.size > 1024 * 1024 * 10) {
        alert('Sorry, this image is too big!');
        return false;
    }
  
    //read file    
    fileReader.readAsDataURL(file);
    fileReader.onload = function () {
        var imageData = this.result;
        crop(imageData);
    };
}


//start 0.3 because the init autoCropArea is 0.7
// its been changed to 0.8 because the calculation is off by 0.1 somewhere
var zoomThrottle = undefined;
var lastZoomVal = 0.3;

var crop = function(img) {
    var $formX = $('#'+options.inputPrefix+'_image_x1');
    var $formY = $('#'+options.inputPrefix+'_image_y1');
    var $formW = $('#'+options.inputPrefix+'_image_w');
    var $formH = $('#'+options.inputPrefix+'_image_h');
    var $cropperCont = undefined;

    modal.open({
        title: 'Crop Image',
        url: '/cropper',
        showBtns: true,
        success: function() {
            var data = $cropperImg.cropper('getData',true);

            //add values to form
            $formX.val( Math.round(data.x) );
            $formY.val( Math.round(data.y) );

            $formW.val( Math.round(data.width) );
            $formH.val( Math.round(data.height) );

            //close modal
            modal.close();
            if (options.callback && typeof options.callback === 'function') {
                options.callback();
            }
        },
        onLoad: function() {
            _cropperLoaded(img);
        },
    });
}

var $cropperImg; // Declare $cropperImg in a wider scope

var _cropperLoaded = function(img) {
    $cropperImg = $('#cropper-image'); // Assign it here
    if(img) {
        $cropperImg
            .attr('src',img)
            .cropper({
                dragMode: 'move',
                cropBoxMovable: true,
                cropBoxResizable: false,
                aspectRatio: options.aspectRatio,
                autoCropArea: 0.8,
                guides: false,
                highlight: false,
                zoomOnWheel: false,
                zoomOnTouch: true,
                responsive: true,
                movable: true,
                viewMode: 1,
                preview: '#'+options.inputPrefix+'-crop-preview'
            });
    }

    //custom zoom
    $('#cropper-zoom').on('change', function() {
        // use the difference.
        var zoomInput = $(this).val();
        var zoomOutput = 0;

        clearTimeout(zoomThrottle);
        zoomThrottle = setTimeout(function(){
            if(zoomInput == lastZoomVal) {
                return false;
            } else if(zoomInput > lastZoomVal) {
                zoomOutput = parseFloat( (zoomInput - lastZoomVal).toFixed(2) );
                //must be positive
                if(zoomOutput < 0) zoomOutput = zoomOutput * -1;
            } else if(zoomInput < lastZoomVal) {
                zoomOutput = parseFloat( (lastZoomVal - zoomInput).toFixed(2) );
                //must be negative
                if(zoomOutput > 0) zoomOutput = zoomOutput * -1;
            }

            //update last zoom
            lastZoomVal = zoomInput;
            //zoom
            $cropperImg.cropper("zoom", zoomOutput);
        }, 50);
    });
}


module.exports = {
    setup: setup,
    drawImage: drawImage
};
